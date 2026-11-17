"""Scan backend for the Neat ADF scanner (USB 1f44:0050).

The scanner speaks a proprietary protocol over one bulk-in (0x81), one
bulk-out (0x02) and one interrupt-in (0x83) endpoint. It is not supported by
SANE. ``NeatBackend`` is the seam where that protocol lives; until it is fully
reverse engineered, :meth:`NeatBackend.scan` raises :class:`ScanNotSupported`
instead of pretending to scan.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .usbip_client import UsbIpError, UsbIpSession


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


class NeatBackend:
    """Native Neat protocol over a USB/IP session (work in progress)."""

    async def scan(self, session: UsbIpSession, dpi: int, color: str) -> list[Page]:
        raise ScanNotSupported(
            "The Neat ADF scan protocol is not implemented yet "
            "(USB/IP link and device discovery work)."
        )


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
