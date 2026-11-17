"""Scanner button event."""
from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity

from .const import DOMAIN
from .entity import NeatEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ScannerButton(hass.data[DOMAIN][entry.entry_id])])


class ScannerButton(NeatEntity, EventEntity):
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = ["pressed"]
    _attr_translation_key = "scanner_button"

    def __init__(self, coordinator):
        super().__init__(coordinator, "scanner_button")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.add_button_listener(self._handle_press))

    def _handle_press(self) -> None:
        self._trigger_event("pressed")
        self.async_write_ha_state()
