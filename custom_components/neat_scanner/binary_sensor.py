"""Online/offline status."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import DOMAIN
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    c = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ScannerOnline(c), PaperPresent(c), CoverOpen(c)])


class ScannerOnline(NeatEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "online"

    def __init__(self, coordinator):
        super().__init__(coordinator, "online")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.get("online"))

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {"server": f"{self.coordinator.host}:{self.coordinator.port}",
                "server_reachable": d.get("reachable", False),
                "busid": self.coordinator.busid}


class _MonitorSensor(NeatEntity, BinarySensorEntity):
    @property
    def available(self) -> bool:
        return self.coordinator.monitor_connected


class PaperPresent(_MonitorSensor):
    _attr_translation_key = "paper"

    def __init__(self, coordinator):
        super().__init__(coordinator, "paper")

    @property
    def is_on(self):
        return self.coordinator.paper


class CoverOpen(_MonitorSensor):
    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_translation_key = "cover"

    def __init__(self, coordinator):
        super().__init__(coordinator, "cover")

    @property
    def is_on(self):
        c = self.coordinator.cover_closed
        return None if c is None else not c
