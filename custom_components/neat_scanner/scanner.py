"""Scan backend for the Neat ADF scanner (USB 1f44:0050).

The scanner is a Genesys GL84x-style device (see docs/PROTOCOL.md). It is not
supported by SANE, so ``NeatBackend`` replays register programs recorded from
the Windows driver (``scripts/*.json``) and decodes the raw sensor stream.
150/200 dpi, grayscale and black and white are derived in software from the
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
SEGMENT_ORDER = (2, 0, 1)  # byte phase -> left, middle, right segment
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
                        raise ScanError("Timed out waiting for scan data (no paper in the feeder?)")
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

    def __init__(self, max_sheets: int = 1, auto_crop: bool = True, duplex: bool = False) -> None:
        self.max_sheets = max_sheets
        self.auto_crop = auto_crop
        self.duplex = duplex

    async def scan(self, session: UsbIpSession, dpi: int, color: str) -> list[Page]:
        loop = asyncio.get_running_loop()
        if self.duplex and dpi > 300:
            raise ScanError("Double-sided scanning supports up to 300 dpi")
        name = "d300" if self.duplex else ("c600" if dpi > 300 else "c300")
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
            if len(raw) // (LINE_BYTES_300 * (6 if self.duplex else 3) * (2 if dpi > 300 else 1)) < 8:
                if sheet == 0:
                    raise ScanError("The scanner returned too little data (is paper loaded in the feeder?)")
                break  # feeder empty: done
            if self.duplex:
                pages += await loop.run_in_executor(None, decode_duplex, raw, dpi, color, self.auto_crop)
            else:
                pages.append(await loop.run_in_executor(None, decode_page, raw, dpi, color, self.auto_crop))
            del raw
            if not more:
                break
        return pages


def _smooth(x, k: int):
    """Moving average along axis 0 (edge padded)."""
    import numpy as np

    pad = k // 2
    xp = np.pad(x, ((pad, pad),) + ((0, 0),) * (x.ndim - 1), mode="edge")
    c = np.cumsum(xp, axis=0, dtype=np.float64)
    c = np.concatenate([np.zeros((1,) + c.shape[1:]), c])
    return ((c[k:] - c[:-k]) / k).astype(np.float32)


def decode_page(raw: bytes, dpi: int, color: str, auto_crop: bool = True, mirror: bool = False) -> Page:
    """Turn the raw sensor stream into an image (blocking; run in an executor).

    The CIS has three segments that are read out in parallel and interleaved byte by
    byte; three consecutive lines are the R, G and B lines of one image row.
    """
    import numpy as np
    from PIL import Image

    scale = 2 if dpi > 300 else 1
    line = LINE_BYTES_300 * scale
    seg = line // 3
    rows = len(raw) // line // 3
    if rows < 8:
        raise ScanError("The scanner returned too little data (is paper loaded in the feeder?)")
    g = np.frombuffer(raw, dtype=np.uint8, count=rows * 3 * line).reshape(rows, 3, seg, 3)
    lead, trail = 20 * scale, 4 * scale
    parts = [g[:, :, lead : seg - trail, tap] for tap in SEGMENT_ORDER]
    img = np.concatenate(parts, axis=2).transpose(0, 2, 1)[::-1, ::-1]  # (rows, width, RGB)
    if mirror:  # the back sensor looks at the sheet from the other side
        img = img[:, ::-1]
    width = img.shape[1]

    # Per column white level (flat-field): the driver calibrates in hardware, we estimate it.
    sample = img[:: max(1, rows // 400)].astype(np.float32)
    white = _smooth(np.percentile(sample, 90, axis=0), 31 * scale)  # (width, 3)
    lum = white.mean(axis=1)
    paper = lum > 0.45 * lum.max()
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
            m = 4 * scale
            r0, r1 = max(0, int(ok[0]) - m), min(rows, int(ok[-1]) + 1 + m)
    img_out = Image.fromarray(np.ascontiguousarray(out[r0:r1]), "RGB")
    base = 600 if dpi > 300 else 300
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


def decode_duplex(raw: bytes, dpi: int, color: str, auto_crop: bool = True) -> list[Page]:
    """Split a duplex stream (6 lines per row: 3 lines of one side, then 3 of the other)."""
    import numpy as np

    line = LINE_BYTES_300 * (2 if dpi > 300 else 1)
    groups = len(raw) // line // 6
    if groups < 8:
        raise ScanError("The scanner returned too little data (is paper loaded in the feeder?)")
    g = np.frombuffer(raw, dtype=np.uint8, count=groups * 6 * line).reshape(groups, 6, line)
    front = np.ascontiguousarray(g[:, 3:6]).tobytes()
    back = np.ascontiguousarray(g[:, 0:3]).tobytes()
    return [
        decode_page(front, dpi, color, auto_crop),
        decode_page(back, dpi, color, auto_crop, mirror=True),
    ]


async def run_scan(
    host: str, port: int, busid: str, dpi: int, color: str,
    backend: Backend | None = None, max_sheets: int = 1, auto_crop: bool = True,
    duplex: bool = False,
) -> list[Page]:
    """Import the remote scanner, scan the ADF and return the pages."""
    backend = backend or NeatBackend(max_sheets, auto_crop, duplex)
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
