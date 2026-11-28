"""Coordinator: polls the USB/IP server and runs scans."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AUTO_SEARCH_AFTER, AUTO_SEARCH_EVERY, CALIBRATION_FILE,
    CONF_AUTO_CROP, CONF_AUTO_ROTATE, CONF_SPLIT_SHEETS, DEFAULT_SPLIT_SHEETS, CONF_DESKEW, DEFAULT_AUTO_ROTATE, DEFAULT_DESKEW, STORAGE_VERSION, CONF_BUSID, CONF_DUPLEX, DEFAULT_DUPLEX, CONF_MONITOR, CONF_SCAN_ON_BUTTON, DEFAULT_MONITOR, DEFAULT_SCAN_ON_BUTTON, EVENT_BUTTON, CONF_COLOR, CONF_DPI, CONF_FORMAT, CONF_HOST, CONF_PORT,
    CONF_SAVE_DIR, CONF_SCAN_INTERVAL, CONF_SHEETS, DEFAULT_AUTO_CROP, DEFAULT_COLOR, DEFAULT_DPI,
    DEFAULT_FORMAT, DEFAULT_SAVE_DIR, DEFAULT_SHEETS, DEFAULT_SCAN_INTERVAL, DOMAIN,
    EVENT_SCAN_COMPLETE, NEAT_PID, NEAT_VID,
)
from .discovery import async_find_scanners
from .scanner import SESSION_SETTLE, ScanError, run_calibration, run_scan, save_pages, watch_sensors
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
        self.duplex: bool = opts.get(CONF_DUPLEX, DEFAULT_DUPLEX)
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
        self._monitor_stop: asyncio.Event | None = None
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_calibration_{entry.entry_id}")
        self._backup_store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_calibration_backup")
        self.calibration: dict = {}
        self._misses = 0
        self._last_search = 0.0
        self._search_lock = asyncio.Lock()

    @property
    def save_dir(self) -> str:
        return {**self.entry.data, **self.entry.options}.get(CONF_SAVE_DIR, DEFAULT_SAVE_DIR)

    @property
    def file_format(self) -> str:
        return {**self.entry.data, **self.entry.options}.get(CONF_FORMAT, DEFAULT_FORMAT)

    @property
    def options(self) -> dict:
        return {**self.entry.data, **self.entry.options}

    async def async_load_calibration(self) -> None:
        self.calibration = await self._store.async_load() or {}
        if not self.calibration:  # a new entry (for example after re-adding): take the last backup
            backup = await self._backup_store.async_load()
            if backup:
                _LOGGER.info("Restored the scanner calibration from the backup")
                self.calibration = backup
                await self._store.async_save(backup)

    async def _async_save_calibration(self) -> None:
        await self._store.async_save(self.calibration)
        await self._backup_store.async_save(self.calibration)

    async def async_export_calibration(self, path: str | None = None) -> str:
        """Write the calibration to a JSON file (default: the save directory) and return the path."""
        if not self.calibration:
            raise HomeAssistantError("There is no calibration to export; run Calibrate first")
        path = path or os.path.join(self.save_dir, CALIBRATION_FILE)
        if not self.hass.config.is_allowed_path(path):
            raise HomeAssistantError(f"{path} is not allowed; add its directory to allowlist_external_dirs")

        def write() -> None:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.calibration, fh)

        await self.hass.async_add_executor_job(write)
        return path

    async def async_import_calibration(self, path: str) -> None:
        """Load a calibration exported earlier (also from another installation)."""
        if not self.hass.config.is_allowed_path(path):
            raise HomeAssistantError(f"{path} is not allowed; add its directory to allowlist_external_dirs")

        def read() -> dict:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)

        try:
            data = await self.hass.async_add_executor_job(read)
        except (OSError, ValueError) as err:
            raise HomeAssistantError(f"Cannot read the calibration file: {err}") from err
        if not isinstance(data, dict) or not isinstance(data.get("front"), dict):
            raise HomeAssistantError("This is not a Neat scanner calibration file")
        self.calibration = data
        await self._async_save_calibration()
        self.async_update_listeners()

    # --- finding the scanner again after the address changed ------------------------------
    def _apply_address(self, host: str, port: int, busid: str) -> None:
        """Store a new address; the update listener reloads the entry."""
        self.host, self.port, self.busid = host, port, busid
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_HOST: host, CONF_PORT: port, CONF_BUSID: busid},
            unique_id=f"{host}_{busid}",
        )

    async def async_find_scanner(self) -> bool:
        """Search the local network for the scanner and, if found, switch the entry to it.

        Returns True when the entry now points at a scanner (it may be reloaded in a moment).
        Raises HomeAssistantError when nothing, or more than one scanner, is found."""
        async with self._search_lock:
            self._last_search = time.monotonic()
            found = await async_find_scanners(self.hass, self.port, [self.host])
            if not found:
                raise HomeAssistantError(
                    "No Neat scanner found on the local network. Check that the phone is on the same "
                    "Wi-Fi and that USBAnywhere is running and sharing the scanner."
                )
            current = (self.host, self.port, self.busid)
            if current in found:
                return True
            if len(found) > 1:
                raise HomeAssistantError(
                    "More than one scanner was found (" + ", ".join(f"{h}:{p} {b}" for h, p, b in found)
                    + "); use Reconfigure to pick one"
                )
            host, port, busid = found[0]
            _LOGGER.warning("Neat scanner found at %s:%s (%s) instead of %s:%s (%s): updating the configuration",
                            host, port, busid, *current)
            self._apply_address(host, port, busid)
            return True

    async def _auto_search(self) -> None:
        try:
            await self.async_find_scanner()
        except HomeAssistantError as err:
            _LOGGER.debug("Automatic scanner search: %s", err)

    @property
    def calibrated_at(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.calibration["created"]).astimezone()
        except (KeyError, ValueError):
            return None

    # --- sensor monitor (paper, cover, scanner button) -------------------------------------
    def start_monitor(self) -> None:
        if self._monitor_task is None and self.options.get(CONF_MONITOR, DEFAULT_MONITOR):
            self._monitor_stop = asyncio.Event()
            self._monitor_task = self.entry.async_create_background_task(
                self.hass, self._monitor_loop(), "neat_scanner sensor monitor"
            )

    async def stop_monitor(self) -> None:
        task, self._monitor_task = self._monitor_task, None
        if task:
            if self._monitor_stop:
                self._monitor_stop.set()  # let it finish the current request, then close cleanly
            try:
                await asyncio.wait_for(asyncio.shield(task), 3)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
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
            await asyncio.sleep(SESSION_SETTLE)  # let the previous session release the scanner
            try:
                await watch_sensors(
                    self.host, self.port, self.busid,
                    self._on_sensor_state, self._on_button, self._set_connected,
                    stop=self._monitor_stop,
                )
                if self._monitor_stop is not None and self._monitor_stop.is_set():
                    return
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
            devices = None
        scanner = None
        if devices is not None:
            scanner = next(
                (d for d in devices if d.busid == self.busid and (d.vid, d.pid) == (NEAT_VID, NEAT_PID)), None
            ) or next((d for d in devices if (d.vid, d.pid) == (NEAT_VID, NEAT_PID)), None)
        if scanner is not None:
            self._misses = 0
            if scanner.busid != self.busid:  # the phone gave the scanner another USB number
                _LOGGER.warning("The scanner is now %s on the server (was %s): updating the configuration",
                                scanner.busid, self.busid)
                self._apply_address(self.host, self.port, scanner.busid)
            return {"online": True, "reachable": True, "serial": None, "busid": scanner.busid}
        self._misses += 1
        if (self._misses >= AUTO_SEARCH_AFTER and not self.scanning and not self._search_lock.locked()
                and time.monotonic() - self._last_search > AUTO_SEARCH_EVERY):
            self.entry.async_create_background_task(self.hass, self._auto_search(), "neat_scanner search")
        return {"online": False, "reachable": devices is not None, "busid": None}

    async def async_scan(
        self, dpi: int | None = None, color: str | None = None,
        save_dir: str | None = None, fmt: str | None = None,
        sheets: int | None = None, auto_crop: bool | None = None,
        duplex: bool | None = None,
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
            if self._monitor_task is not None:  # only one USB/IP session can hold the scanner
                await self.stop_monitor()
                await asyncio.sleep(SESSION_SETTLE)
            try:
                opts = {**self.entry.data, **self.entry.options}
                pages = await run_scan(
                    self.host, self.port, self.busid, dpi or self.dpi, color or self.color,
                    max_sheets=sheets or opts.get(CONF_SHEETS, DEFAULT_SHEETS),
                    auto_crop=opts.get(CONF_AUTO_CROP, DEFAULT_AUTO_CROP) if auto_crop is None else auto_crop,
                    duplex=self.duplex if duplex is None else duplex,
                    calibration=self.calibration,
                    deskew=opts.get(CONF_DESKEW, DEFAULT_DESKEW),
                    rotate=opts.get(CONF_AUTO_ROTATE, DEFAULT_AUTO_ROTATE),
                    split_sheets=opts.get(CONF_SPLIT_SHEETS, DEFAULT_SPLIT_SHEETS),
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

    async def async_calibrate(self) -> None:
        """Scan a blank white sheet and store the per column white reference."""
        if self._lock.locked():
            raise HomeAssistantError("A scan is already in progress")
        async with self._lock:
            self.scanning, self.last_error = True, None
            self.async_update_listeners()
            if self._monitor_task is not None:
                await self.stop_monitor()
                await asyncio.sleep(SESSION_SETTLE)
            try:
                cal = await run_calibration(self.host, self.port, self.busid)
            except ScanError as err:
                self.last_error = str(err)
                raise HomeAssistantError(str(err)) from err
            finally:
                self.scanning = False
                self.async_update_listeners()
                self.start_monitor()
        self.calibration = cal
        await self._async_save_calibration()
        self.async_update_listeners()
