from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.echolocal import personal, triggers

NODE = "office_echo"
ALEX = {"match": "known", "name": "Alex", "person": "person.alex"}


@pytest.fixture
async def rig(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, list[dict[str, Any]]] = {"notify": [], "todo": [], "calendar": []}

    async def ring(call: ServiceCall):
        calls["notify"].append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_alex_phone", ring)

    async def add(call: ServiceCall):
        calls["todo"].append(dict(call.data))

    hass.services.async_register("todo", "add_item", add)

    async def events(call: ServiceCall):
        calls["calendar"].append(dict(call.data))
        start = dt_util.start_of_local_day().replace(hour=9)
        return {
            "calendar.alex": {
                "events": [
                    {"start": start.replace(hour=14).isoformat(), "summary": "Dentist"},
                    {"start": start.isoformat(), "summary": "Standup"},
                    {"start": start.date().isoformat(), "summary": "Holiday"},
                ]
            }
        }

    hass.services.async_register(
        "calendar", "get_events", events, supports_response=SupportsResponse.ONLY
    )
    monkeypatch.setattr(personal, "phone_service", _phone)

    hass.states.async_set("person.alex", "home", {"friendly_name": "Alex"})
    await personal.async_setup(hass)
    return calls


def _phone(hass: HomeAssistant, who: personal.Asker) -> str:
    return "mobile_app_alex_phone"


async def ask(hass: HomeAssistant, operation: str, who: Any = ALEX, **values: Any) -> str | None:
    return await personal.answer(hass, "dev1", who, {"operation": operation, **values})


async def test_an_unknown_voice_gets_nothing_personal(hass: HomeAssistant, rig):
    speech = await ask(hass, "add", {"match": "unknown"}, item="milk")
    assert speech == "I don't know whose voice that is."
    assert rig["todo"] == []


async def test_a_voice_with_no_person_is_told_so(hass: HomeAssistant, rig):
    speech = await ask(hass, "ring", {**ALEX, "person": ""})
    assert speech == "Alex, your voice isn't linked to a person yet."


async def test_an_operation_nobody_handles_is_no_answer(hass: HomeAssistant, rig):
    assert await ask(hass, "dance") is None


async def test_a_missing_setting_names_what_is_missing(hass: HomeAssistant, rig):
    assert await ask(hass, "add", item="milk") == "Alex, there's no list set up for you."


async def test_items_go_on_the_speakers_own_list(hass: HomeAssistant, rig):
    hass.data[personal.DATA]["people"]["person.alex"] = {"list": "todo.alex"}
    assert await ask(hass, "add", item="milk") == "Added milk to your list."
    assert rig["todo"] == [{"entity_id": "todo.alex", "item": "milk"}]


async def test_the_calendar_reads_the_day_in_order(hass: HomeAssistant, rig):
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    speech = await ask(hass, "calendar", day="today")
    assert speech == (
        "You have 3 things today: Holiday all day, Standup at 9:00 AM, Dentist at 2:00 PM."
    )
    assert rig["calendar"][0]["entity_id"] == ["calendar.alex"]


async def test_find_my_phone_rings_loudly(hass: HomeAssistant, rig):
    assert await ask(hass, "ring") == "Ringing your phone."
    sent = rig["notify"][0]
    assert sent["data"]["channel"] == "alarm_stream"
    assert sent["data"]["push"]["sound"]["critical"] == 1


async def test_a_reminder_arrives_after_its_wait_and_survives_until_then(hass: HomeAssistant, rig):
    speech = await ask(hass, "remind", task="call mom", minutes="10", hours="")
    assert speech == "I'll remind you in 10 minutes."
    assert len(hass.data[personal.DATA]["reminders"]) == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert rig["notify"] == []

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert rig["notify"] == [{"message": "call mom"}]
    assert hass.data[personal.DATA]["reminders"] == []


async def test_hours_are_spoken_as_hours(hass: HomeAssistant, rig):
    assert await ask(hass, "remind", task="stretch", hours="2", minutes="") == (
        "I'll remind you in 2 hours."
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2, minutes=1))
    await hass.async_block_till_done()
    assert rig["notify"] == [{"message": "stretch"}]


async def test_a_declared_personal_action_is_answered_here(
    hass: HomeAssistant, rig, monkeypatch: pytest.MonkeyPatch
):
    hass.data[personal.DATA]["people"]["person.alex"] = {"list": "todo.alex"}
    monkeypatch.setattr(triggers, "_node", lambda hass, device_id: NODE)
    sent: list[dict[str, Any]] = []

    async def device(call: ServiceCall):
        sent.append(dict(call.data))
        return {"answer": ALEX}

    hass.services.async_register(
        "esphome", f"{NODE}_personal", device, supports_response=SupportsResponse.ONLY
    )
    t = triggers.Triggers(hass)
    t.devices["dev1"] = triggers.Declared(
        actions={
            "personal": {
                "name": "personal",
                "args": [
                    {"name": "operation", "type": "string"},
                    {"name": "item", "type": "string"},
                ],
            }
        },
        at=0,
    )
    reply = await t._run("dev1", "personal", {"operation": "add", "item": "milk"}, "en")
    assert reply == "Added milk to your list."
    assert sent == [{"operation": "add", "item": "milk"}]
    assert rig["todo"] == [{"entity_id": "todo.alex", "item": "milk"}]
