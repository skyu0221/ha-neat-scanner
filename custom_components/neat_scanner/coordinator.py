"""Coordinator: polls the USB/IP server and runs scans."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_AUTO_CROP, CONF_BUSID, CONF_MONITOR, CONF_SCAN_ON_BUTTON, DEFAULT_MONITOR, DEFAULT_SCAN_ON_BUTTON, EVENT_BUTTON, CONF_COLOR, CONF_DPI, CONF_FORMAT, CONF_HOST, CONF_PORT,
    CONF_SAVE_DIR, CONF_SCAN_INTERVAL, CONF_SHEETS, DEFAULT_AUTO_CROP, DEFAULT_COLOR, DEFAULT_DPI,
    DEFAULT_FORMAT, DEFAULT_SAVE_DIR, DEFAULT_SHEETS, DEFAULT_SCAN_INTERVAL, DOMAIN,
    EVENT_SCAN_COMPLETE, NEAT_PID, NEAT_VID,
)
from .scanner import ScanError, run_scan, save_pages, watch_sensors
from .usbip_client import UsbIpError, list_devices

_LOGGER = logging.getLogger(__name__)


class NeatScannerCoordinator(DataUpdateCoordinator[dict]):
    """Tracks scanner availability and holds the current scan settings."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        opts = {**entry.data, **entry.options}
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        )
        self.host = entry.data[CONF_HOST]
        self.port = entry.data[CONF_PORT]
        self.busid = entry.data[CONF_BUSID]
        self.dpi: int = opts.get(CONF_DPI, DEFAULT_DPI)
        self.color: str = opts.get(CONF_COLOR, DEFAULT_COLOR)
        self.scanning = False
        self.last_scan: datetime | None = None
        self.last_files: list[str] = []
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self.paper: bool | None = None
        self.cover_closed: bool | None = None
        self.monitor_connected = False
        self._monitor_task: asyncio.Task | None = None
        self._button_listeners: list = []

    @property
    def save_dir(self) -> str:
        return {**self.entry.data, **self.entry.options}.get(CONF_SAVE_DIR, DEFAULT_SAVE_DIR)

    @property
    def file_format(self) -> str:
        return {**self.entry.data, **self.entry.options}.get(CONF_FORMAT, DEFAULT_FORMAT)

    @property
    def options(self) -> dict:
        return {**self.entry.data, **self.entry.options}

    # --- sensor monitor (paper, cover, scanner button) -------------------------------------
    def start_monitor(self) -> None:
        if self._monitor_task is None and self.options.get(CONF_MONITOR, DEFAULT_MONITOR):
            self._monitor_task = self.entry.async_create_background_task(
                self.hass, self._monitor_loop(), "neat_scanner sensor monitor"
            )

    async def stop_monitor(self) -> None:
        task, self._monitor_task = self._monitor_task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._set_connected(False)

    def add_button_listener(self, callback):
        self._button_listeners.append(callback)
        return lambda: self._button_listeners.remove(callback)

    def _set_connected(self, connected: bool) -> None:
        if self.monitor_connected != connected:
            self.monitor_connected = connected
            if not connected:
                self.paper = self.cover_closed = None
            self.async_update_listeners()

    def _on_sensor_state(self, paper: bool, cover_closed: bool) -> None:
        self.paper, self.cover_closed = paper, cover_closed
        self.async_update_listeners()

    def _on_button(self) -> None:
        _LOGGER.debug("Scanner button pressed")
        self.hass.bus.async_fire(EVENT_BUTTON, {"entry_id": self.entry.entry_id})
        for callback in list(self._button_listeners):
            callback()
        if self.options.get(CONF_SCAN_ON_BUTTON, DEFAULT_SCAN_ON_BUTTON) and not self._lock.locked():
            self.entry.async_create_background_task(self.hass, self._scan_from_button(), "neat_scanner button scan")

    async def _scan_from_button(self) -> None:
        try:
            await self.async_scan()
        except HomeAssistantError as err:
            _LOGGER.warning("Scan started by the scanner button failed: %s", err)

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await watch_sensors(
                    self.host, self.port, self.busid,
                    self._on_sensor_state, self._on_button, self._set_connected,
                )
            except UsbIpError as err:
                _LOGGER.debug("Sensor monitor link lost: %s", err)
            self._set_connected(False)
            await asyncio.sleep(10)

    async def _async_update_data(self) -> dict:
        """Online = server reachable AND the Neat scanner is exported."""
        if self.monitor_connected:  # the persistent session proves it is online
            return {"online": True, "reachable": True, "busid": self.busid}
        try:
            devices = await list_devices(self.host, self.port, timeout=4)
        except UsbIpError:
            return {"online": False, "reachable": False}
        scanner = next(
            (d for d in devices if d.busid == self.busid or (d.vid, d.pid) == (NEAT_VID, NEAT_PID)),
            None,
        )
        return {"online": scanner is not None, "reachable": True,
                "serial": None, "busid": scanner.busid if scanner else None}

    async def async_scan(
        self, dpi: int | None = None, color: str | None = None,
        save_dir: str | None = None, fmt: str | None = None,
        sheets: int | None = None, auto_crop: bool | None = None,
    ) -> list[str]:
        """Scan the ADF, save the pages, fire an event and return the paths."""
        if self._lock.locked():
            raise HomeAssistantError("A scan is already in progress")
        directory = save_dir or self.save_dir
        if not self.hass.config.is_allowed_path(directory):
            raise HomeAssistantError(
                f"Save directory {directory} is not allowed; add it to "
                "allowlist_external_dirs in configuration.yaml"
            )
        async with self._lock:
            self.scanning, self.last_error = True, None
            self.async_update_listeners()
            await self.stop_monitor()  # only one USB/IP session can hold the scanner
            try:
                opts = {**self.entry.data, **self.entry.options}
                pages = await run_scan(
                    self.host, self.port, self.busid, dpi or self.dpi, color or self.color,
                    max_sheets=sheets or opts.get(CONF_SHEETS, DEFAULT_SHEETS),
                    auto_crop=opts.get(CONF_AUTO_CROP, DEFAULT_AUTO_CROP) if auto_crop is None else auto_crop,
                )
                files = await self.hass.async_add_executor_job(
                    save_pages, pages, directory, fmt or self.file_format
                )
            except ScanError as err:
                self.last_error = str(err)
                raise HomeAssistantError(str(err)) from err
            finally:
                self.scanning = False
                self.async_update_listeners()
                self.start_monitor()
        self.last_scan, self.last_files = datetime.now(timezone.utc), files
        self.hass.bus.async_fire(EVENT_SCAN_COMPLETE, {"files": files, "pages": len(pages)})
        self.async_update_listeners()
        return files
