"""Scan status and last scan sensors."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .const import DOMAIN
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ScanStatus(c), LastScan(c)])


class ScanStatus(NeatEntity, SensorEntity):
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["offline", "idle", "scanning", "error"]

    def __init__(self, coordinator):
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str:
        c = self.coordinator
        if c.scanning:
            return "scanning"
        if not (c.data and c.data.get("online")):
            return "offline"
        return "error" if c.last_error else "idle"

    @property
    def extra_state_attributes(self):
        return {"last_error": self.coordinator.last_error}


class LastScan(NeatEntity, SensorEntity):
    _attr_translation_key = "last_scan"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator):
        super().__init__(coordinator, "last_scan")

    @property
    def native_value(self):
        return self.coordinator.last_scan

    @property
    def extra_state_attributes(self):
        return {"files": self.coordinator.last_files,
                "save_dir": self.coordinator.save_dir}
