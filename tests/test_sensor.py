from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.echolocal.const import DOMAIN
from custom_components.echolocal.ping import PROTOCOLS, VERSION_ENTITY


async def test_protocol_version_is_a_registered_entity(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, options={"sidebar": False})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(VERSION_ENTITY)
    assert state is not None
    assert state.state == str(max(PROTOCOLS))
    assert er.async_get(hass).async_get(VERSION_ENTITY).unique_id == "protocol_version"

    assert await hass.config_entries.async_unload(entry.entry_id)
