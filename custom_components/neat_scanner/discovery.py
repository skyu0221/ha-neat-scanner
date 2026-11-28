"""Find the Neat scanner on the local network (a USB/IP server on TCP 3240 that shares it)."""
from __future__ import annotations

import asyncio
import ipaddress
import logging

from homeassistant.components import network
from homeassistant.core import HomeAssistant

from .const import DEFAULT_PORT, NEAT_PID, NEAT_VID
from .usbip_client import UsbIpError, list_devices

_LOGGER = logging.getLogger(__name__)

MAX_HOSTS = 1024  # never probe more than this many addresses
CONNECT_TIMEOUT = 0.7
CONCURRENCY = 128


async def _local_hosts(hass: HomeAssistant, first: list[str]) -> list[str]:
    """Addresses to probe: ``first`` (known ones) then every host of the local IPv4 networks."""
    hosts: list[str] = list(dict.fromkeys(first))
    own: set[str] = set()
    for adapter in await network.async_get_adapters(hass):
        if not adapter.get("enabled"):
            continue
        for ip4 in adapter.get("ipv4", []):
            address, prefix = ip4["address"], int(ip4["network_prefix"])
            ip = ipaddress.ip_address(address)
            if ip.is_loopback or ip.is_link_local or prefix >= 31:
                continue
            own.add(address)
            net = ipaddress.ip_network(f"{address}/{max(prefix, 22)}", strict=False)  # at most 1022 hosts
            for host in net.hosts():
                h = str(host)
                if h not in own and h not in hosts and len(hosts) < MAX_HOSTS:
                    hosts.append(h)
    return hosts


async def _probe(host: str, port: int, sem: asyncio.Semaphore) -> list[tuple[str, int, str]]:
    async with sem:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return []
        writer.close()
    try:
        devices = await list_devices(host, port, timeout=4)
    except UsbIpError:
        return []
    return [(host, port, d.busid) for d in devices if (d.vid, d.pid) == (NEAT_VID, NEAT_PID)]


async def async_find_scanners(
    hass: HomeAssistant, port: int = DEFAULT_PORT, known_hosts: list[str] | None = None,
) -> list[tuple[str, int, str]]:
    """Return ``[(host, port, busid), ...]`` for every shared Neat ADF scanner on the local network."""
    hosts = await _local_hosts(hass, known_hosts or [])
    _LOGGER.debug("Looking for the scanner on %d addresses (port %d)", len(hosts), port)
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_probe(h, port, sem) for h in hosts))
    found = [item for sub in results for item in sub]
    _LOGGER.debug("Scanner search result: %s", found)
    return found
