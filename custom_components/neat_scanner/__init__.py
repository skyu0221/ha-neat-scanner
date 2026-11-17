"""Neat ADF scanner integration (over USB/IP, e.g. USBAnywhere on Android)."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import COLOR_OPTIONS, DOMAIN, DPI_MAX, DPI_MIN, FORMAT_OPTIONS, SERVICE_SCAN
from .coordinator import NeatScannerCoordinator

PLATFORMS = ["binary_sensor", "sensor", "select", "button", "event"]

SCAN_SCHEMA = vol.Schema(
    {
        vol.Optional("dpi"): vol.All(vol.Coerce(int), vol.Range(min=DPI_MIN, max=DPI_MAX)),
        vol.Optional("color"): vol.In(COLOR_OPTIONS),
        vol.Optional("format"): vol.In(FORMAT_OPTIONS),
        vol.Optional("save_dir"): cv.string,
        vol.Optional("sheets"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional("auto_crop"): cv.boolean,
        vol.Optional("entry_id"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = NeatScannerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    coordinator.start_monitor()

    if not hass.services.has_service(DOMAIN, SERVICE_SCAN):

        async def _scan(call: ServiceCall) -> dict:
            coords = hass.data[DOMAIN]
            target = coords.get(call.data.get("entry_id")) or next(iter(coords.values()))
            files = await target.async_scan(
                call.data.get("dpi"), call.data.get("color"),
                call.data.get("save_dir"), call.data.get("format"),
                call.data.get("sheets"), call.data.get("auto_crop"),
            )
            return {"files": files}

        hass.services.async_register(
            DOMAIN, SERVICE_SCAN, _scan, schema=SCAN_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await hass.data[DOMAIN][entry.entry_id].stop_monitor()
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SCAN)
    return ok
