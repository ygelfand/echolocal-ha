from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .ping import PROTOCOLS, VERSION_ENTITY


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ProtocolVersionSensor()])


class ProtocolVersionSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "protocol_version"
    _attr_unique_id = "protocol_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_native_value = str(max(PROTOCOLS))

    def __init__(self) -> None:
        self.entity_id = VERSION_ENTITY
