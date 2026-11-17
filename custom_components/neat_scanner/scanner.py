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
import os
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .usbip_client import UsbIpError, UsbIpSession

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
LINE_BYTES_300 = 2646  # one raw sensor line at the 300 dpi script
CHUNK_UNIT = 12  # data counter register unit in bytes
POLL_TIMEOUT = 180.0  # give up when no data arrives for this long (seconds)
MAX_DELAY_MS = 2000


def _load_script(name: str) -> dict:
    with open(os.path.join(SCRIPT_DIR, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


async def _play(session: UsbIpSession, ops: list, timeout: float = 15.0) -> None:
    """Replay captured control/bulk operations, keeping their original timing."""
    for op in ops:
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


class NeatBackend:
    """Neat protocol over USB/IP: replays captured register programs and decodes the stream."""

    async def scan(self, session: UsbIpSession, dpi: int, color: str) -> list[Page]:
        loop = asyncio.get_running_loop()
        name = "c600" if dpi > 300 else "c300"
        script = await loop.run_in_executor(None, _load_script, name)
        chunk = script["chunk"]
        raw = bytearray()
        scan_error: Exception | None = None
        try:
            await _play(session, script["prelude"])
            last_data = loop.time()
            while True:
                status, avail = await _poll_counter(session)
                finished = (status & 0xC0) == 0xC0
                if avail >= chunk:
                    raw += await _read_chunk(session, chunk)
                    last_data = loop.time()
                elif finished:
                    if avail:
                        raw += await _read_chunk(session, avail)
                    break
                else:
                    if loop.time() - last_data > POLL_TIMEOUT:
                        raise ScanError("Timed out waiting for scan data (no paper in the feeder?)")
                    await asyncio.sleep(0.05)
        except ScanError as err:
            scan_error = err
        except UsbIpError as err:
            scan_error = ScanError(f"USB/IP error during scan: {err}")
        finally:
            try:  # always try to park the motor / stop the scan
                await _play(session, script["tail"], timeout=10)
            except UsbIpError:
                pass
        if scan_error:
            raise scan_error
        page = await loop.run_in_executor(None, decode_page, bytes(raw), dpi, color)
        return [page]


def decode_page(raw: bytes, dpi: int, color: str) -> Page:
    """Turn the raw sensor stream into an image (blocking; run in an executor)."""
    import numpy as np
    from PIL import Image

    scale = 2 if dpi > 300 else 1
    px = 882 * scale
    line = LINE_BYTES_300 * scale
    rows = len(raw) // line // 3
    if rows < 8:
        raise ScanError("The scanner returned too little data (is paper loaded in the feeder?)")
    g = np.frombuffer(raw, dtype=np.uint8, count=rows * 3 * line).reshape(rows, 3, px, 3)
    phase = int(np.argmax([g[::25, :, :, i].mean() for i in range(3)]))
    # One of every three bytes carries the sample; consecutive lines are R, G, B.
    rgb = np.ascontiguousarray(g[:, :, :, phase].transpose(0, 2, 1)[::-1, ::-1])
    img = Image.fromarray(rgb, "RGB")
    base = 600 if dpi > 300 else 300
    if dpi != base:
        factor = dpi / base
        img = img.resize((round(img.width * factor), round(img.height * factor)), Image.Resampling.BOX)
    if color in ("grayscale", "bw"):
        img = img.convert("L")
        if color == "bw":
            img = img.point(lambda v: 255 if v >= 128 else 0).convert("1")
    return Page(img.width, img.height, img.mode, img.tobytes())


async def run_scan(
    host: str, port: int, busid: str, dpi: int, color: str,
    backend: Backend | None = None,
) -> list[Page]:
    """Import the remote scanner, scan the ADF and return the pages."""
    backend = backend or NeatBackend()
    try:
        async with UsbIpSession(host, busid, port) as session:
            return await backend.scan(session, dpi, color)
    except UsbIpError as err:
        raise ScanError(str(err)) from err


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
