"""Scan backend for the Neat ADF scanner (USB 1f44:0050).

The scanner is a Genesys GL84x-style device (see docs/PROTOCOL.md). It is not
supported by SANE, so ``NeatBackend`` replays register programs recorded from
the Windows driver (``scripts/*.json``) and decodes the raw sensor stream.
any dpi up to 300, grayscale and black and white are derived in software from the
300/600 dpi color scans.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .usbip_client import UsbIpError, UsbIpSession

_LOGGER = logging.getLogger(__name__)

EP_IN = 0x81
EP_OUT = 0x02


class ScanError(Exception):
    """Scanning failed."""


class ScanNotSupported(ScanError):
    """The Neat scan protocol is not implemented yet."""


@dataclass
class Page:
    """One scanned page as raw pixels."""

    width: int
    height: int
    mode: str  # "RGB", "L" or "1"
    data: bytes


class Backend(Protocol):
    async def scan(self, session: UsbIpSession, dpi: int, color: str) -> list[Page]: ...


SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "scripts")
LINE_BYTES_300 = 2646  # one raw sensor line at the 300 dpi script (3 segments x 882)
SEGMENT_ORDER = (2, 0, 1)
# script name -> (bytes per sensor line, dummy px at segment start, at end, native dpi)
GEOMETRY = {
    "c600": (5292, 40, 8, 600),
    "c300": (2646, 20, 4, 300),
    "d300": (2646, 20, 4, 300),
    "g300": (2646, 20, 4, 300),
}
GRAY_MODES = {"g300"}  # one sensor line per image row instead of R, G, B lines  # byte phase -> left, middle, right segment
CHUNK_UNIT = 12  # data counter register unit in bytes
POLL_TIMEOUT = 180.0  # give up when no data arrives for this long (seconds)
MAX_DELAY_MS = 2000
SESSION_SETTLE = 2.0  # the phone needs this long to hand the scanner to the next USB/IP session
STALL_SECS = 4.0  # data counter unchanged this long after data arrived: the sheet is complete
NEXT_SHEET_TIMEOUT = 25.0  # after the first sheet: no data for this long means the feeder is empty


def _load_script(name: str) -> dict:
    with open(os.path.join(SCRIPT_DIR, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


async def _play(session: UsbIpSession, ops: list, timeout: float = 15.0) -> None:
    """Replay captured control/bulk operations, keeping their original timing."""
    for n, op in enumerate(ops):
        try:
            if op[0] == "c":
                setup = bytes.fromhex(op[1])
                rtype, req, val, idx, length = struct.unpack("<BBHHH", setup)
                if op[3]:
                    await asyncio.sleep(min(op[3], MAX_DELAY_MS) / 1000)
                if rtype & 0x80:
                    await session.control(rtype, req, val, idx, length, timeout=timeout)
                else:
                    await session.control(rtype, req, val, idx, 0, bytes.fromhex(op[2]), timeout=timeout)
            elif op[0] == "bo":
                await session.bulk_write(EP_OUT, bytes.fromhex(op[1]), timeout=timeout)
            elif op[0] == "bi":
                await session.bulk_read(EP_IN, op[1], timeout=timeout)
        except UsbIpError as err:
            raise UsbIpError(f"{err} (step {n}/{len(ops)}: {op[0]} {str(op[1])[:24]})") from err


async def _read_chunk(session: UsbIpSession, length: int, timeout: float = 20.0) -> bytes:
    """Request ``length`` bytes from the scanner RAM and read them like the Windows driver."""
    hdr = bytes([0, 0, 0, 0x10]) + struct.pack("<I", length)
    await session.control(0x40, 0x04, 0x82, 0, 0, hdr, timeout=timeout)
    first = length // 512 * 512
    out = b""
    if first:
        out += await session.bulk_read(EP_IN, first, timeout=timeout)
    if length - first:
        out += await session.bulk_read(EP_IN, length - first, timeout=timeout)
    return out


async def _poll_counter(session: UsbIpSession) -> tuple[int, int]:
    """Return (status register 0x40, bytes waiting in scanner RAM)."""
    d = await session.control(0xC0, 0x04, 0x8E, 0x4022, 16, timeout=10)
    return d[0], int.from_bytes(d[2:6], "big") * CHUNK_UNIT


async def _paper_flags(session: UsbIpSession) -> dict:
    """Read the sensor bits: paper at the feeder, cover closed, button (pulse)."""
    d = await session.control(0xC0, 0x04, 0x8E, 0x4022, 4, timeout=5)
    g = await session.control(0xC0, 0x0C, 0x8E, 0x21, 1, timeout=5)
    return {
        "paper": not (d[0] & 0x80 or d[1] & 0x08),  # both bits are 1 when the feeder is empty
        "cover_closed": bool(d[0] & 0x20),
        "button": bool(g[0] & 0x08),
        "raw": (d[0], d[1], g[0]),
    }


async def watch_sensors(
    host: str, port: int, busid: str, on_state, on_button, on_connected, interval: float = 0.25,
    stop: asyncio.Event | None = None,
) -> None:
    """Poll paper, cover and button on a persistent USB/IP session until cancelled.

    The button is a pulse of about 0.5 s, so the interval must stay below 0.3 s. Raises
    UsbIpError when the link drops.
    """
    async with UsbIpSession(host, busid, port) as session:
        on_connected(True)
        prev = None
        prev_button = False
        while True:
            flags = await _paper_flags(session)
            # The paper reading flickers while the cover is open: keep the last value then.
            paper = flags["paper"] if flags["cover_closed"] or prev is None else prev[0]
            state = (paper, flags["cover_closed"])
            if state != prev:
                prev = state
                on_state(*state)
            if flags["button"] and not prev_button:
                on_button()
            prev_button = flags["button"]
            if stop is None:
                await asyncio.sleep(interval)
            else:  # leave between two requests so no transfer is cut off
                try:
                    await asyncio.wait_for(stop.wait(), interval)
                    return
                except asyncio.TimeoutError:
                    pass


async def _scan_sheet(session: UsbIpSession, script: dict, first: bool) -> bytes:
    """Feed and scan one sheet; returns the raw sensor stream (may be empty after the last sheet)."""
    loop = asyncio.get_running_loop()
    chunk = script["chunk"]
    raw = bytearray()
    scan_error: Exception | None = None
    limit = POLL_TIMEOUT if first else NEXT_SHEET_TIMEOUT
    try:
        await _play(session, script["prelude"])
        last_data = loop.time()
        polls = 0
        last_avail, last_change = -1, loop.time()
        while True:
            status, avail = await _poll_counter(session)
            finished = (status & 0xC0) == 0xC0
            polls += 1
            if polls % 20 == 1 or finished:
                _LOGGER.info("scan poll: status=%02x avail=%d read=%d", status, avail, len(raw))
            if avail != last_avail:
                last_avail, last_change = avail, loop.time()
            if avail >= chunk:
                raw += await _read_chunk(session, chunk)
                last_data = last_change = loop.time()
                last_avail = -1
            else:
                stalled = (raw or avail) and loop.time() - last_change > STALL_SECS
                if finished or stalled:
                    if stalled and not finished:
                        _LOGGER.info("scan stalled (next sheet waiting?): treating sheet as complete")
                    if avail:
                        raw += await _read_chunk(session, avail)
                    break
                if loop.time() - last_data > limit:
                    if first:
                        raise ScanError(
                            "Timed out waiting for scan data. If the scanner light keeps blinking, unplug the "
                            "scanner, plug it in again and re-share it in USBAnywhere, then retry."
                        )
                    break
                await asyncio.sleep(0.05)
    except ScanError as err:
        scan_error = err
    except UsbIpError as err:
        scan_error = ScanError(f"USB/IP error during scan: {err}")
    if scan_error:
        raise scan_error
    return bytes(raw)


async def _finish_sheet(session: UsbIpSession, script: dict, more: bool) -> None:
    """Stop sequence. With another sheet waiting the driver skips the eject step, otherwise the
    paper would be pushed out and the next sheet pulled in too far (its leading part is lost)."""
    try:
        await _play(session, script["tail_between"] if more else script["tail"], timeout=10)
    except UsbIpError:
        pass


class NeatBackend:
    """Neat protocol over USB/IP: replays captured register programs and decodes the stream."""

    def __init__(
        self, max_sheets: int = 1, auto_crop: bool = True, duplex: bool = False,
        calibration: dict | None = None, deskew: bool = False, rotate: bool = False,
        split_sheets: bool = True,
    ) -> None:
        self.split_sheets = split_sheets
        self.max_sheets = max_sheets
        self.auto_crop = auto_crop
        self.duplex = duplex
        self.calibration = calibration or {}
        self.deskew = deskew
        self.rotate = rotate

    def _decode_sheets(self, raw: bytes, dpi: int, color: str, name: str) -> list[Page]:
        """Decode one scan stream; a continuous program may contain several sheets."""
        line = GEOMETRY[name][0]
        bpr = line * (1 if name in GRAY_MODES else 3)
        return [
            decode_page(
                raw[a * bpr : b * bpr], dpi, color, self.auto_crop, mode=name,
                calibration=self.calibration.get("front"), deskew=self.deskew, rotate=self.rotate,
            )
            for a, b in sheet_ranges(raw, name, self.split_sheets)
        ]

    async def calibrate(self, session: UsbIpSession) -> dict:
        """Scan a blank white sheet (double-sided pass) and return the white profiles."""
        loop = asyncio.get_running_loop()
        script = await loop.run_in_executor(None, _load_script, "d300")
        if not (await _paper_flags(session))["paper"]:
            raise ScanError("No paper in the feeder")
        try:
            raw = await _scan_sheet(session, script, first=True)
        except ScanError:
            await _finish_sheet(session, script, False)
            raise
        await _finish_sheet(session, script, False)

        def build() -> dict:
            front, back = split_duplex(raw)
            return {
                "front": white_profile(front, "d300"),
                "back": white_profile(back, "d300", mirror=True),
                "created": datetime.now().isoformat(timespec="seconds"),
            }

        return await loop.run_in_executor(None, build)

    async def scan(self, session: UsbIpSession, dpi: int, color: str) -> list[Page]:
        loop = asyncio.get_running_loop()
        if self.duplex and dpi > 300:
            raise ScanError("Double-sided scanning supports up to 300 dpi")
        name = _mode_for(dpi, self.duplex, color)
        script = await loop.run_in_executor(None, _load_script, name)
        pages: list[Page] = []
        if not (await _paper_flags(session))["paper"]:
            raise ScanError("No paper in the feeder")
        for sheet in range(self.max_sheets):
            try:
                raw = await _scan_sheet(session, script, first=sheet == 0)
            except ScanError:
                await _finish_sheet(session, script, False)
                raise
            more = sheet + 1 < self.max_sheets and (await _paper_flags(session))["paper"]
            await _finish_sheet(session, script, more)
            line = GEOMETRY[name][0]
            lines = 6 if self.duplex else (1 if name in GRAY_MODES else 3)
            if len(raw) // (line * lines) < 8:
                if sheet == 0:
                    raise ScanError("The scanner returned too little data (is paper loaded in the feeder?)")
                break  # feeder empty: done
            if self.duplex:
                pages += await loop.run_in_executor(
                    None, decode_duplex, raw, dpi, color, self.auto_crop, self.calibration, self.deskew,
                    self.rotate, self.split_sheets,
                )
            else:
                pages += await loop.run_in_executor(None, self._decode_sheets, raw, dpi, color, name)
            del raw
            if not more:
                break
        per_sheet = 2 if self.duplex else 1
        if len(pages) > self.max_sheets * per_sheet:  # a continuous program scans the whole stack
            _LOGGER.warning("Scanned %d sheets, keeping the first %d", len(pages) // per_sheet, self.max_sheets)
            pages = pages[: self.max_sheets * per_sheet]
        return pages


def _mode_for(dpi: int, duplex: bool = False, color: str = "color") -> str:
    """Pick the captured scan program that is closest to (and not below) the requested dpi."""
    if duplex:
        return "d300"
    if dpi > 300:
        return "c600"
    if color in ("grayscale", "bw"):
        return "g300"  # a third of the data of colour, so faster
    return "c300"  # anything up to 300 dpi is scaled down from the 300 dpi scan


# Measured file bytes per pixel on real receipt scans (almost independent of the dpi)
BYTES_PER_PIXEL = {
    ("png", "color"): 1.9, ("png", "grayscale"): 0.61, ("png", "bw"): 0.02,
    ("jpeg", "color"): 0.18, ("jpeg", "grayscale"): 0.18, ("jpeg", "bw"): 0.18,
    ("pdf", "color"): 0.18, ("pdf", "grayscale"): 0.18, ("pdf", "bw"): 0.18,
}


def estimate_size(dpi: int, color: str, fmt: str) -> dict:
    """Expected pixels and file size of a US Letter page (8.5 x 11 in) and of a 3.1 x 10 in receipt."""
    bpp = BYTES_PER_PIXEL.get((fmt, color), 1.0)
    letter = round(8.5 * dpi) * round(11 * dpi)
    receipt = round(3.1 * dpi) * round(10 * dpi)
    return {
        "letter_pixels": f"{round(8.5 * dpi)} x {round(11 * dpi)}",
        "letter_mpx": round(letter / 1e6, 1),
        "letter_mb": round(letter * bpp / 1e6, 2),
        "receipt_pixels": f"{round(3.1 * dpi)} x {round(10 * dpi)}",
        "receipt_mb": round(receipt * bpp / 1e6, 2),
    }


CONTINUOUS_MODES = {"c300", "d300", "g300"}  # programs that keep feeding until the feeder is empty
MIN_SHEET_ROWS = 300  # a sheet is at least this tall (1 inch at 300 dpi); shorter pieces are joined
SKIP_COLS = 20  # sensor columns (every 3rd) left out when looking for paper
MAX_GAP_ROWS = 80  # a longer dip is not the gap between two sheets
MIN_PAPER_PX = 60  # paper at least this wide (every 3rd column, about 0.6 in) around a gap
EDGE_SHIFT = 3  # the paper edges of two sheets differ by at least this many columns (every 3rd)
GAP_ROWS = 2  # a gap between two sheets is at least this many rows with (almost) no paper (3 to 14 seen)


def sheet_ranges(raw: bytes, mode: str, split: bool = True) -> list[tuple[int, int]]:
    """Split a continuous multi-sheet stream into per-sheet row ranges (in scan order).

    With these programs the scanner pulls the next sheet in on its own, so the stream has no
    page marker; between two sheets there is a short gap (3 to 17 rows at 300 dpi) in which the
    sensor sees almost no paper, or at least the paper edges jump (another width or position, for
    example a small receipt behind a normal one). A crease or a printed black band inside a sheet
    also dips but keeps the paper edges, so it is not a gap. Returns ``[(first_row, last_row_exclusive), ...]`` in units of
    sensor rows (R, G, B lines or one gray line, or the front side of a duplex stream).
    """
    import numpy as np

    line = GEOMETRY[mode][0]
    nl = 1 if mode in GRAY_MODES else 3
    rows = len(raw) // line // nl
    if rows < 8 or mode not in CONTINUOUS_MODES or not split:
        return [(0, rows)]
    img = _assemble(raw[: rows * nl * line], mode)[::-1, ::3, 1].astype(np.float32)  # scan order, every 3rd column, green
    sub = img[:, SKIP_COLS:]  # the first sensor columns read about the same with and without paper
    white = float(np.percentile(sub[:: max(1, rows // 300)], 97))
    bright = sub > 0.7 * white
    cnt = bright.sum(axis=1).astype(np.int32)  # paper pixels per row, wherever the paper is
    width = bright.shape[1]
    left = bright.argmax(axis=1)
    right = width - 1 - bright[:, ::-1].argmax(axis=1)
    padded = np.pad(cnt, 30, mode="edge")
    typical = np.median(np.lib.stride_tricks.sliding_window_view(padded, 61), axis=1)  # paper width around a row
    dip = cnt < 0.5 * typical
    cuts, r = [], 0
    while r < rows:
        if not dip[r]:
            r += 1
            continue
        e = r
        while e < rows and dip[e]:
            e += 1
        if GAP_ROWS <= e - r <= MAX_GAP_ROWS and r > 35 and rows - e > 35:
            before, after = slice(r - 35, r - 3), slice(e + 3, min(rows, e + 35))
            tb, ta = float(np.median(cnt[before])), float(np.median(cnt[after]))
            typ = min(tb, ta)
            if typ >= MIN_PAPER_PX:
                ok_b, ok_a = cnt[before] >= 0.5 * tb, cnt[after] >= 0.5 * ta
                shift = max(
                    abs(float(np.median(left[before][ok_b])) - float(np.median(left[after][ok_a]))),
                    abs(float(np.median(right[before][ok_b])) - float(np.median(right[after][ok_a]))),
                )
                low = int(cnt[r:e].min())
                # Two sheets: the sensor sees (almost) no paper, or the paper edges jump. A crease or a
                # printed black band also dips but the paper edges stay where they were.
                if low < 0.03 * typ or shift >= EDGE_SHIFT:
                    cuts.append((r + e) // 2)
                    _LOGGER.info("sheet gap at row %d: %d rows, paper %d of %.0f px, edge shift %.1f px",
                                 r, e - r, low, typ, shift)
                else:
                    _LOGGER.debug("not a sheet gap: rows %d-%d, paper %d of %.0f px, edge shift %.1f px",
                                  r, e, low, typ, shift)
        r = e
    bounds = [0] + cuts + [rows]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        if (cnt[a:b] >= 25).sum() >= 30:  # skip blank stretches
            out.append((a, b))
    # A piece shorter than MIN_SHEET_ROWS is no sheet: it is a printed rule or a glitch that looked like a
    # gap, so join it to its neighbour instead of losing it.
    merged: list[list[int]] = []
    for a, b in out:
        if merged and (b - a < MIN_SHEET_ROWS or merged[-1][1] - merged[-1][0] < MIN_SHEET_ROWS):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged] or [(0, rows)]


def _smooth(x, k: int):
    """Moving average along axis 0 (edge padded)."""
    import numpy as np

    pad = k // 2
    xp = np.pad(x, ((pad, pad),) + ((0, 0),) * (x.ndim - 1), mode="edge")
    c = np.cumsum(xp, axis=0, dtype=np.float64)
    c = np.concatenate([np.zeros((1,) + c.shape[1:]), c])
    return ((c[k:] - c[:-k]) / k).astype(np.float32)


def _assemble(raw: bytes, mode: str, mirror: bool = False):
    """Sensor stream -> image array (rows, width, 3) before any level correction.

    The CIS has three segments that are read out in parallel and interleaved byte by byte;
    three consecutive lines are the R, G and B lines of one image row (gray: one line).
    """
    import numpy as np

    line, lead, trail, _base = GEOMETRY[mode]
    nl = 1 if mode in GRAY_MODES else 3  # sensor lines per image row
    seg = line // 3
    rows = len(raw) // line // nl
    if rows < 8:
        raise ScanError("The scanner returned too little data (is paper loaded in the feeder?)")
    g = np.frombuffer(raw, dtype=np.uint8, count=rows * nl * line).reshape(rows, nl, seg, 3)
    parts = [g[:, :, lead : seg - trail, tap] for tap in SEGMENT_ORDER]
    img = np.concatenate(parts, axis=2).transpose(0, 2, 1)[::-1, ::-1]  # (rows, width, RGB or 1)
    if nl == 1:
        img = np.repeat(img, 3, axis=2)
    if mirror:  # the back sensor looks at the sheet from the other side
        img = img[:, ::-1]
    return img


CAL_WIDTH = 2574  # columns of the 300 dpi color scan: calibration profiles are stored at this width
CAL_MIN_WHITE = 200  # a usable white reference: at least this bright over most of the width


def white_profile(raw: bytes, mode: str, mirror: bool = False) -> list:
    """Per column white level (RGB) of a scan of a blank white sheet, at the 300 dpi color width."""
    import numpy as np

    img = _assemble(raw, mode, mirror)
    rows = img.shape[0]
    mid = img[rows // 5 : rows * 4 //5 : 2].astype(np.float32)
    prof = np.median(mid, axis=0)  # (width, 3)
    ok = (prof.mean(axis=1) > CAL_MIN_WHITE).mean()
    if ok < 0.85:
        raise ScanError(
            "Calibration needs a clean blank white letter-size sheet that covers the full width "
            f"(only {ok:.0%} of the columns were bright enough)"
        )
    x = np.linspace(0, prof.shape[0] - 1, CAL_WIDTH)
    prof = np.stack([np.interp(x, np.arange(prof.shape[0]), prof[:, c]) for c in range(3)], axis=1)
    return np.round(prof, 1).tolist()


def _reference(cal: list, width: int, mode: str):
    """Calibration profile resampled to ``width`` columns (gray modes use the luminance)."""
    import numpy as np

    p = np.asarray(cal, np.float32)
    if mode in GRAY_MODES:
        p = np.repeat(p.mean(axis=1, keepdims=True), 3, axis=1)
    if width != len(p):
        x = np.linspace(0, len(p) - 1, width)
        p = np.stack([np.interp(x, np.arange(len(p)), p[:, c]) for c in range(3)], axis=1)
    return _smooth(p, 3)


MAX_SKEW = 5.0  # degrees searched by the deskew step
ORIENT_MIN = 0.2  # |orientation score| needed before a page is turned upside down


def _text_mask(img, max_side: int = 1000):
    """Small binary image (dark = 1) of the page content for skew estimation."""
    import numpy as np
    from PIL import Image

    g = img.convert("L")
    f = min(1.0, max_side / max(g.size))
    if f < 1.0:
        g = g.resize((max(1, int(g.width * f)), max(1, int(g.height * f))), Image.Resampling.BOX)
    a = np.asarray(g, dtype=np.float32)
    paper = np.percentile(a, 80)
    return (a < 0.7 * paper).astype(np.float32), f


def estimate_skew(img) -> float:
    """Skew angle in degrees (counter-clockwise positive: rotate the image by -angle to fix).

    Projection profile: when the text lines are horizontal the row sums are the most uneven.
    Returns 0.0 when there is not enough text to be sure.
    """
    import numpy as np
    from PIL import Image

    mask, _f = _text_mask(img)
    if mask.sum() < 200:
        return 0.0
    m = Image.fromarray((mask * 255).astype(np.uint8))

    def score(angle: float) -> float:
        r = np.asarray(m.rotate(angle, resample=Image.Resampling.NEAREST, expand=False), dtype=np.float32)
        prof = r.sum(axis=1)
        return float(np.sum(np.diff(prof) ** 2))

    coarse = [(score(a), a) for a in np.arange(-MAX_SKEW, MAX_SKEW + 1e-6, 0.5)]
    best = max(coarse)[1]
    fine = [(score(a), a) for a in np.arange(best - 0.5, best + 0.5 + 1e-6, 0.1)]
    s_best, a_best = max(fine)
    s_zero = score(0.0)
    typical = float(np.median([c[0] for c in coarse]))
    if s_best < 1.1 * typical or abs(a_best) < 0.15 or s_best < 1.02 * s_zero:
        return 0.0
    return float(a_best)


def orientation_score(img) -> float:
    """Positive: text is upright, negative: upside down, near 0: unknown.

    In Latin text there is more ink above the main text band (ascenders, capitals) than below
    the baseline (descenders), so the row profile of each text line is lopsided.
    """
    import numpy as np

    mask, _f = _text_mask(img, 1800)
    p = mask.sum(axis=1)
    if p.max() < 5:
        return 0.0
    k = 3
    ps = np.convolve(p, np.ones(k) / k, mode="same")
    on = ps > 0.12 * np.percentile(ps, 95)
    up = down = 0.0
    lines = 0
    i, n = 0, len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j]:
            j += 1
        q = p[i:j]
        if 6 <= len(q) <= 80 and q.max() > 0:
            core = np.where(q >= 0.5 * q.max())[0]
            up += q[: core[0]].sum()
            down += q[core[-1] + 1 :].sum()
            lines += 1
        i = j
    if lines < 8 or up + down < 1:
        return 0.0
    return float((up - down) / (up + down))


def trim_to_paper(img):
    """Crop away the dark background wedges left after rotating a skewed sheet."""
    import numpy as np
    from PIL import Image

    g = img.convert("L")
    f = min(1.0, 800 / max(g.size))
    sm = g.resize((max(1, int(g.width * f)), max(1, int(g.height * f))), Image.Resampling.BOX) if f < 1 else g
    a = np.asarray(sm, dtype=np.float32)
    paper = a > 0.7 * np.percentile(a, 90)
    cols = np.where(paper.mean(axis=0) > 0.5)[0]
    rows = np.where(paper.mean(axis=1) > 0.5)[0]
    if len(cols) < 8 or len(rows) < 8:
        return img
    box = (int(cols[0] / f), int(rows[0] / f), int((cols[-1] + 1) / f), int((rows[-1] + 1) / f))
    return img.crop(box)


def deskew_image(img):
    """Straighten a slightly rotated page (blocking)."""
    from PIL import Image

    angle = estimate_skew(img)
    if angle == 0.0:
        return img
    _LOGGER.debug("Deskew by %.1f degrees", angle)
    rotated = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(40, 40, 40))
    return trim_to_paper(rotated)


def decode_page(
    raw: bytes, dpi: int, color: str, auto_crop: bool = True, mirror: bool = False,
    mode: str | None = None, calibration: list | None = None, deskew: bool = False,
    rotate: bool = False,
) -> Page:
    """Turn the raw sensor stream into an image (blocking; run in an executor).

    ``calibration`` is a white profile (see ``white_profile``) for this side of the sheet. Without
    one the white level per column is estimated from the page itself.
    """
    import numpy as np
    from PIL import Image

    mode = mode or _mode_for(dpi)
    base = GEOMETRY[mode][3]
    scale = base / 300
    img = _assemble(raw, mode, mirror)
    rows, width = img.shape[:2]

    # Per column white level (flat-field): the driver calibrates in hardware, we use a stored
    # white sheet scan or estimate it from the page.
    if calibration:
        white = _reference(calibration, width, mode)
        sample = img[:: max(1, rows // 400)].astype(np.float32)
        # 97th percentile: a skewed sheet covers a column over only part of its length. Columns
        # whose white reference is dim (the sensor edge) read the same with and without paper.
        norm = np.percentile(sample, 97, axis=0) / np.maximum(white, 1.0)
        paper = (norm.mean(axis=1) > 0.7) & (white.mean(axis=1) > CAL_MIN_WHITE)
        gain = np.clip(245.0 / np.maximum(white, 1.0), 0.7, 3.0).astype(np.float32)
        # The white sheet saturates in some sensor segments (the true level is unknown there), so
        # level the three segments against each other using the page's own white.
        white90 = np.percentile(sample, 90, axis=0) / np.maximum(white, 1.0)  # (width, 3)
        edges = [0, width // 3, 2 * width // 3, width]
        levels = []
        for k in range(3):
            cols = np.where(paper[edges[k] : edges[k + 1]])[0] + edges[k]
            levels.append(np.median(white90[cols], axis=0) if len(cols) >= 40 else None)
        have = [lv for lv in levels if lv is not None]
        if have:
            ref = np.median(np.stack(have), axis=0)
            if len(have) >= 2:
                for k, lv in enumerate(levels):
                    if lv is not None:
                        ratio = ref / np.maximum(lv, 0.05)
                        if np.all(np.abs(ratio - 1) < 0.2):
                            gain[edges[k] : edges[k + 1]] *= ratio.astype(np.float32)
            if np.all((ref > 0.75) & (ref < 1.1)):  # the page's own paper is the white point
                gain /= ref.astype(np.float32)
    else:
        sample = img[:: max(1, rows // 400)].astype(np.float32)
        white = _smooth(np.percentile(sample, 90, axis=0), max(5, int(31 * scale)) | 1)  # (width, 3)
        lum = white.mean(axis=1)
        lum97 = _smooth(np.percentile(sample, 97, axis=0), max(5, int(31 * scale)) | 1).mean(axis=1)
        paper = lum97 > 0.7 * lum97.max()
        gain = np.ones((width, 3), np.float32)
        gain[paper] = np.clip(245.0 / np.maximum(white[paper], 1.0), 0.8, 2.5)
    c0, c1 = (int(np.argmax(paper)), int(width - np.argmax(paper[::-1]))) if paper.any() else (0, width)
    if not auto_crop:
        c0, c1 = 0, width
    out = np.empty((rows, c1 - c0, 3), np.uint8)
    step = 512
    for r0 in range(0, rows, step):
        blk = img[r0 : r0 + step, c0:c1].astype(np.float32) * gain[c0:c1]
        out[r0 : r0 + step] = np.clip(blk, 0, 255).astype(np.uint8)
    r0, r1 = 0, rows
    if auto_crop:
        rowlum = out[:, :: max(1, (c1 - c0) // 200)].mean(axis=(1, 2))
        ok = np.where(rowlum > 0.5 * np.percentile(rowlum, 90))[0]
        if len(ok) > 8:
            m = max(2, int(4 * scale))
            r0, r1 = max(0, int(ok[0]) - m), min(rows, int(ok[-1]) + 1 + m)
    img_out = Image.fromarray(np.ascontiguousarray(out[r0:r1]), "RGB")
    if deskew:
        img_out = deskew_image(img_out)
    if rotate and orientation_score(img_out) <= -ORIENT_MIN:
        img_out = img_out.transpose(Image.Transpose.ROTATE_180)
    if dpi != base:
        factor = dpi / base
        img_out = img_out.resize(
            (max(1, round(img_out.width * factor)), max(1, round(img_out.height * factor))),
            Image.Resampling.BOX,
        )
    if color in ("grayscale", "bw"):
        img_out = img_out.convert("L")
        if color == "bw":
            img_out = img_out.point(lambda v: 255 if v >= 128 else 0).convert("1")
    return Page(img_out.width, img_out.height, img_out.mode, img_out.tobytes())


def split_duplex(raw: bytes) -> tuple[bytes, bytes]:
    """Duplex stream -> (front, back) raw single-sided streams."""
    import numpy as np

    line = GEOMETRY["d300"][0]
    groups = len(raw) // line // 6
    if groups < 8:
        raise ScanError("The scanner returned too little data (is paper loaded in the feeder?)")
    g = np.frombuffer(raw, dtype=np.uint8, count=groups * 6 * line).reshape(groups, 6, line)
    return np.ascontiguousarray(g[:, 3:6]).tobytes(), np.ascontiguousarray(g[:, 0:3]).tobytes()


def decode_duplex(
    raw: bytes, dpi: int, color: str, auto_crop: bool = True, calibration: dict | None = None,
    deskew: bool = False, rotate: bool = False, split_sheets: bool = True,
) -> list[Page]:
    """Split a duplex stream (6 lines per row: 3 lines of one side, then 3 of the other)."""
    front, back = split_duplex(raw)
    cal = calibration or {}
    bpr = GEOMETRY["d300"][0] * 3
    pages: list[Page] = []
    for a, b in sheet_ranges(front, "d300", split_sheets):
        fa, fb = a * bpr, b * bpr
        pages.append(decode_page(front[fa:fb], dpi, color, auto_crop, mode="d300",
                                 calibration=cal.get("front"), deskew=deskew, rotate=rotate))
        pages.append(decode_page(back[fa:fb], dpi, color, auto_crop, mirror=True, mode="d300",
                                 calibration=cal.get("back"), deskew=deskew, rotate=rotate))
    return pages


async def run_scan(
    host: str, port: int, busid: str, dpi: int, color: str,
    backend: Backend | None = None, max_sheets: int = 1, auto_crop: bool = True,
    duplex: bool = False, calibration: dict | None = None, deskew: bool = False,
    rotate: bool = False, split_sheets: bool = True,
) -> list[Page]:
    """Import the remote scanner, scan the ADF and return the pages."""
    backend = backend or NeatBackend(max_sheets, auto_crop, duplex, calibration, deskew, rotate, split_sheets)
    for attempt in (1, 2):
        try:
            async with UsbIpSession(host, busid, port) as session:
                return await backend.scan(session, dpi, color)
        except UsbIpError as err:
            raise ScanError(str(err)) from err
        except ScanError as err:
            # A request stalled right after the session started: the scanner was not handed over
            # yet. Nothing has been scanned at that point, so wait and try once more.
            if attempt == 1 and "status=-32" in str(err) and re.search(r"step [01]/", str(err)):
                _LOGGER.debug("Scanner not ready yet, retrying: %s", err)
                await asyncio.sleep(SESSION_SETTLE)
                continue
            raise
    raise ScanError("unreachable")


def save_pages(pages: list[Page], directory: str, fmt: str, prefix: str = "scan") -> list[str]:
    """Write pages to ``directory``; returns the created file paths (blocking)."""
    from PIL import Image  # bundled with Home Assistant

    if not pages:
        raise ScanError("The scanner returned no pages (ADF empty?).")
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    images = [Image.frombytes(p.mode, (p.width, p.height), p.data) for p in pages]
    if fmt == "pdf":
        path = os.path.join(directory, f"{prefix}_{stamp}.pdf")
        first, rest = images[0].convert("RGB"), [i.convert("RGB") for i in images[1:]]
        first.save(path, "PDF", save_all=True, append_images=rest)
        return [path]
    ext = "jpg" if fmt == "jpeg" else "png"
    paths = []
    for n, img in enumerate(images, 1):
        path = os.path.join(directory, f"{prefix}_{stamp}_p{n}.{ext}")
        (img.convert("RGB") if ext == "jpg" else img).save(path)
        paths.append(path)
    return paths


async def run_calibration(host: str, port: int, busid: str) -> dict:
    """Import the scanner and measure the white reference from a blank white sheet."""
    for attempt in (1, 2):
        try:
            async with UsbIpSession(host, busid, port) as session:
                return await NeatBackend().calibrate(session)
        except UsbIpError as err:
            raise ScanError(str(err)) from err
        except ScanError as err:
            if attempt == 1 and "status=-32" in str(err) and re.search(r"step [01]/", str(err)):
                await asyncio.sleep(SESSION_SETTLE)
                continue
            raise
    raise ScanError("unreachable")
