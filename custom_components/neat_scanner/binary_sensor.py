"""Online/offline status."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import DOMAIN
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ScannerOnline(hass.data[DOMAIN][entry.entry_id])])


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
