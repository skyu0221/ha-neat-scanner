"""Neat ADF scanner integration (over USB/IP, e.g. USBAnywhere on Android)."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from homeassistant.helpers.storage import Store

from .const import (
    COLOR_OPTIONS, DOMAIN, DPI_MAX, DPI_MIN, FORMAT_OPTIONS, SERVICE_CALIBRATE, SERVICE_EXPORT_CALIBRATION,
    SERVICE_IMPORT_CALIBRATION, SERVICE_SCAN, STORAGE_VERSION,
)
from .coordinator import NeatScannerCoordinator

PLATFORMS = ["binary_sensor", "sensor", "select", "number", "button", "event", "switch"]

SCAN_SCHEMA = vol.Schema(
    {
        vol.Optional("dpi"): vol.All(vol.Coerce(int), vol.Range(min=DPI_MIN, max=DPI_MAX)),
        vol.Optional("color"): vol.In(COLOR_OPTIONS),
        vol.Optional("format"): vol.In(FORMAT_OPTIONS),
        vol.Optional("save_dir"): cv.string,
        vol.Optional("sheets"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional("auto_crop"): cv.boolean,
        vol.Optional("duplex"): cv.boolean,
        vol.Optional("entry_id"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = NeatScannerCoordinator(hass, entry)
    await coordinator.async_load_calibration()
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    coordinator.start_monitor()

    async def _on_stop(_event: Event) -> None:
        # HA shutdown does not unload entries; close the USB/IP session cleanly so the scanner
        # is not left with a cut-off transfer (it would need a replug).
        await coordinator.stop_monitor()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))

    if not hass.services.has_service(DOMAIN, SERVICE_SCAN):

        async def _scan(call: ServiceCall) -> dict:
            coords = hass.data[DOMAIN]
            target = coords.get(call.data.get("entry_id")) or next(iter(coords.values()))
            files = await target.async_scan(
                call.data.get("dpi"), call.data.get("color"),
                call.data.get("save_dir"), call.data.get("format"),
                call.data.get("sheets"), call.data.get("auto_crop"), call.data.get("duplex"),
            )
            return {"files": files}

        hass.services.async_register(
            DOMAIN, SERVICE_SCAN, _scan, schema=SCAN_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )

        async def _calibrate(call: ServiceCall) -> None:
            coords = hass.data[DOMAIN]
            target = coords.get(call.data.get("entry_id")) or next(iter(coords.values()))
            await target.async_calibrate()

        hass.services.async_register(
            DOMAIN, SERVICE_CALIBRATE, _calibrate,
            schema=vol.Schema({vol.Optional("entry_id"): cv.string}),
        )

        async def _export(call: ServiceCall) -> dict:
            coords = hass.data[DOMAIN]
            target = coords.get(call.data.get("entry_id")) or next(iter(coords.values()))
            return {"path": await target.async_export_calibration(call.data.get("path"))}

        hass.services.async_register(
            DOMAIN, SERVICE_EXPORT_CALIBRATION, _export,
            schema=vol.Schema({vol.Optional("path"): cv.string, vol.Optional("entry_id"): cv.string}),
            supports_response=SupportsResponse.OPTIONAL,
        )

        async def _import(call: ServiceCall) -> None:
            coords = hass.data[DOMAIN]
            target = coords.get(call.data.get("entry_id")) or next(iter(coords.values()))
            await target.async_import_calibration(call.data["path"])

        hass.services.async_register(
            DOMAIN, SERVICE_IMPORT_CALIBRATION, _import,
            schema=vol.Schema({vol.Required("path"): cv.string, vol.Optional("entry_id"): cv.string}),
        )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Keep the calibration as a backup that the next entry picks up (the entry id changes)."""
    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_calibration_{entry.entry_id}")
    data = await store.async_load()
    if data:
        await Store(hass, STORAGE_VERSION, f"{DOMAIN}_calibration_backup").async_save(data)
    await store.async_remove()


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await hass.data[DOMAIN][entry.entry_id].stop_monitor()
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SCAN)
            hass.services.async_remove(DOMAIN, SERVICE_CALIBRATE)
            hass.services.async_remove(DOMAIN, SERVICE_EXPORT_CALIBRATION)
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_CALIBRATION)
    return ok
