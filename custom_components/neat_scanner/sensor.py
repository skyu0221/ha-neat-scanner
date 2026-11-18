"""Scan status and last scan sensors."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory

from .const import DOMAIN
from .scanner import estimate_size
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ScanStatus(c), LastScan(c), Calibration(c), EstimatedSize(c)])


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


class Calibration(NeatEntity, SensorEntity):
    """When the white reference was last measured (unknown = never calibrated)."""

    _attr_translation_key = "calibration"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "calibration")

    @property
    def native_value(self):
        return self.coordinator.calibrated_at


class EstimatedSize(NeatEntity, SensorEntity):
    """Expected file size of one US Letter page at the selected resolution, color mode and format."""

    _attr_translation_key = "estimated_size"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = "MB"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator):
        super().__init__(coordinator, "estimated_size")

    def _estimate(self) -> dict:
        c = self.coordinator
        return estimate_size(c.dpi, c.color, c.file_format)

    @property
    def native_value(self) -> float:
        return self._estimate()["letter_mb"]

    @property
    def extra_state_attributes(self):
        e = self._estimate()
        return {
            "letter_pixels": e["letter_pixels"], "letter_megapixels": e["letter_mpx"],
            "receipt_mb": e["receipt_mb"], "receipt_pixels": e["receipt_pixels"],
            "note": "Estimate from real receipt scans; busy pages compress less.",
        }
