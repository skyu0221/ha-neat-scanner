"""Minimal asyncio USB/IP client (no root, no vhci_hcd needed).

Talks the USB/IP wire protocol directly to a USBAnywhere / usbipd server
(TCP 3240), so Home Assistant can drive a remote USB device from inside an
unprivileged container.
"""
from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

USBIP_PORT = 3240
OP_VERSION = 0x0111
OP_REQ_DEVLIST = 0x8005
OP_REP_DEVLIST = 0x0005
OP_REQ_IMPORT = 0x8003
OP_REP_IMPORT = 0x0003
CMD_SUBMIT = 0x00000001
CMD_UNLINK = 0x00000002
RET_SUBMIT = 0x00000003
RET_UNLINK = 0x00000004
DIR_OUT = 0
DIR_IN = 1

_DEV_FMT = ">256s32sIIIHHHBBBBBB"
_DEV_LEN = struct.calcsize(_DEV_FMT)  # 312


class UsbIpError(Exception):
    """USB/IP protocol or transport error."""


@dataclass
class UsbIpDevice:
    path: str
    busid: str
    busnum: int
    devnum: int
    speed: int
    vid: int
    pid: int
    bcd_device: int
    dev_class: int
    dev_subclass: int
    dev_protocol: int
    config_value: int
    num_configs: int
    num_interfaces: int

    @property
    def devid(self) -> int:
        return (self.busnum << 16) | self.devnum


def _parse_dev(raw: bytes) -> UsbIpDevice:
    f = struct.unpack(_DEV_FMT, raw)
    return UsbIpDevice(
        f[0].split(b"\0", 1)[0].decode(errors="replace"),
        f[1].split(b"\0", 1)[0].decode(errors="replace"),
        *f[2:],
    )


async def _open(host: str, port: int, timeout: float):
    try:
        return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError) as err:
        raise UsbIpError(f"cannot connect to {host}:{port}: {err}") from err


async def list_devices(host: str, port: int = USBIP_PORT, timeout: float = 5.0):
    """Return the devices a server exports (OP_REQ_DEVLIST)."""
    reader, writer = await _open(host, port, timeout)
    try:
        writer.write(struct.pack(">HHI", OP_VERSION, OP_REQ_DEVLIST, 0))
        await writer.drain()
        _, code, status = struct.unpack(">HHI", await asyncio.wait_for(reader.readexactly(8), timeout))
        if code != OP_REP_DEVLIST or status:
            raise UsbIpError(f"devlist failed (code={code:#x}, status={status})")
        (count,) = struct.unpack(">I", await asyncio.wait_for(reader.readexactly(4), timeout))
        devices = []
        for _ in range(count):
            dev = _parse_dev(await asyncio.wait_for(reader.readexactly(_DEV_LEN), timeout))
            await asyncio.wait_for(reader.readexactly(4 * dev.num_interfaces), timeout)
            devices.append(dev)
        return devices
    except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as err:
        raise UsbIpError(f"devlist error: {err}") from err
    finally:
        writer.close()


class UsbIpSession:
    """An imported remote USB device. Use as an async context manager."""

    def __init__(self, host: str, busid: str, port: int = USBIP_PORT, timeout: float = 10.0):
        self.host, self.busid, self.port, self.timeout = host, busid, port, timeout
        self.device: UsbIpDevice | None = None
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._seq = 0
        self._dirs: dict[int, int] = {}

    async def __aenter__(self) -> "UsbIpSession":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> UsbIpDevice:
        self._r, self._w = await _open(self.host, self.port, self.timeout)
        try:
            self._w.write(
                struct.pack(">HHI32s", OP_VERSION, OP_REQ_IMPORT, 0, self.busid.encode())
            )
            await self._w.drain()
            _, code, status = struct.unpack(
                ">HHI", await asyncio.wait_for(self._r.readexactly(8), self.timeout)
            )
            if code != OP_REP_IMPORT or status:
                raise UsbIpError(f"import {self.busid} refused (status={status}); busy or not shared?")
            self.device = _parse_dev(
                await asyncio.wait_for(self._r.readexactly(_DEV_LEN), self.timeout)
            )
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as err:
            await self.close()
            raise UsbIpError(f"import error: {err}") from err
        except UsbIpError:
            await self.close()
            raise
        return self.device

    async def close(self) -> None:
        if self._w:
            self._w.close()
            try:
                await self._w.wait_closed()
            except OSError:
                pass
        self._r = self._w = None

    async def _submit(
        self, ep: int, direction: int, length: int, setup: bytes = b"\0" * 8,
        data: bytes = b"", timeout: float | None = None,
    ) -> bytes:
        if not self._w or not self.device:
            raise UsbIpError("session not open")
        self._seq += 1
        seq = self._seq
        self._dirs[seq] = direction
        hdr = (
            struct.pack(">IIIII", CMD_SUBMIT, seq, self.device.devid, direction, ep)
            + struct.pack(">IiiiI", 0, length, 0, -1, 0)
            + setup
        )
        self._w.write(hdr + (data if direction == DIR_OUT else b""))
        await self._w.drain()
        tmo = timeout or self.timeout
        try:
            return await self._wait_ret(seq, direction, tmo)
        except asyncio.TimeoutError as err:
            await self._unlink(seq)
            raise UsbIpError("URB timed out") from err
        except (OSError, asyncio.IncompleteReadError) as err:
            raise UsbIpError(f"URB error: {err!r}") from err

    async def _wait_ret(self, seq: int, direction: int, tmo: float) -> bytes:
        """Read replies until the one for ``seq`` arrives (raises TimeoutError)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + tmo
        while True:
            left = max(0.01, deadline - loop.time())
            basic = await asyncio.wait_for(self._r.readexactly(20), left)
            cmd, rseq = struct.unpack(">II", basic[:8])
            rest = await asyncio.wait_for(self._r.readexactly(28), 5)
            status, actual = struct.unpack(">ii", rest[:8])
            payload = b""
            if cmd == RET_SUBMIT and rseq == seq and direction == DIR_IN and actual > 0:
                payload = await asyncio.wait_for(self._r.readexactly(actual), 5)
            elif cmd == RET_SUBMIT and rseq != seq and actual > 0 and self._dirs.get(rseq) == DIR_IN:
                await asyncio.wait_for(self._r.readexactly(actual), 5)
            if cmd == RET_SUBMIT and rseq == seq:
                if status:
                    raise UsbIpError(f"URB failed: status={status}")
                return payload

    async def _unlink(self, victim: int) -> None:
        """Cancel a pending URB so it cannot block later transfers."""
        self._seq += 1
        useq = self._seq
        hdr = struct.pack(">IIIII", CMD_UNLINK, useq, self.device.devid, 0, 0)
        self._w.write(hdr + struct.pack(">I", victim) + b"\0" * 24)
        await self._w.drain()
        try:
            while True:
                basic = await asyncio.wait_for(self._r.readexactly(20), 3)
                cmd, rseq = struct.unpack(">II", basic[:8])
                rest = await asyncio.wait_for(self._r.readexactly(28), 3)
                actual = struct.unpack(">i", rest[4:8])[0]
                if cmd == RET_SUBMIT and rseq == victim and actual > 0 and self._dirs.get(victim) == DIR_IN:
                    await asyncio.wait_for(self._r.readexactly(actual), 3)
                if cmd == RET_UNLINK and rseq == useq:
                    return
        except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            return

    async def control(
        self, request_type: int, request: int, value: int, index: int,
        length: int = 0, data: bytes = b"", timeout: float | None = None,
    ) -> bytes:
        """Control transfer on EP0. Direction comes from bit 7 of request_type."""
        setup = struct.pack("<BBHHH", request_type, request, value, index, length or len(data))
        direction = DIR_IN if request_type & 0x80 else DIR_OUT
        return await self._submit(0, direction, length or len(data), setup, data, timeout)

    async def bulk_read(self, ep: int, length: int, timeout: float | None = None) -> bytes:
        return await self._submit(ep & 0x0F, DIR_IN, length, timeout=timeout)

    async def bulk_write(self, ep: int, data: bytes, timeout: float | None = None) -> None:
        await self._submit(ep & 0x0F, DIR_OUT, len(data), data=data, timeout=timeout)

    async def get_descriptor(self, dtype: int, index: int = 0, length: int = 255, lang: int = 0) -> bytes:
        return await self.control(0x80, 0x06, (dtype << 8) | index, lang, length)
