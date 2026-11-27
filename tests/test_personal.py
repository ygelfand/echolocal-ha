from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import intent
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.echolocal import personal

NODE = "office_echo"


class Device:
    id = "dev1"


@pytest.fixture
async def rig(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch):
    said: dict[str, Any] = {"match": "known", "name": "Alex", "person": "person.alex"}
    calls: dict[str, list[dict[str, Any]]] = {"notify": [], "todo": [], "calendar": []}

    monkeypatch.setattr(personal, "_node", lambda hass, device_id: NODE if device_id else None)

    async def turn_speaker(call: ServiceCall):
        return said

    hass.services.async_register(
        "esphome", f"{NODE}_turn_speaker", turn_speaker, supports_response=SupportsResponse.ONLY
    )

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
    return said, calls


def _phone(hass: HomeAssistant, who: personal.Asker) -> str:
    if who.record.get("phone") is None and who.person != "person.alex":
        raise personal.Refusal("no phone")
    return "mobile_app_alex_phone"


async def ask(hass: HomeAssistant, intent_type: str, **slots: Any) -> str:
    response = await intent.async_handle(
        hass,
        "test",
        intent_type,
        {name: {"value": value} for name, value in slots.items()},
        device_id="dev1",
    )
    return response.speech["plain"]["speech"]


async def test_an_unknown_voice_gets_nothing_personal(hass: HomeAssistant, rig):
    said, calls = rig
    said.clear()
    said["match"] = "unknown"
    assert (
        await ask(hass, "EchoLocalAddToMyList", item="milk") == "I don't know whose voice that is."
    )
    assert calls["todo"] == []


async def test_a_voice_with_no_person_is_told_so(hass: HomeAssistant, rig):
    said, _ = rig
    said["person"] = ""
    speech = await ask(hass, "EchoLocalFindMyPhone")
    assert speech == "Alex, your voice isn't linked to a person yet."


async def test_no_device_means_no_speaker(hass: HomeAssistant, rig):
    assert await personal.speaker(hass, None) == {"match": "none"}


async def test_a_missing_setting_names_what_is_missing(hass: HomeAssistant, rig):
    assert await ask(hass, "EchoLocalAddToMyList", item="milk") == (
        "Alex, there's no list set up for you."
    )


async def test_items_go_on_the_speakers_own_list(hass: HomeAssistant, rig):
    _, calls = rig
    hass.data[personal.DATA]["people"]["person.alex"] = {"list": "todo.alex"}
    assert await ask(hass, "EchoLocalAddToMyList", item="milk") == "Added milk to your list."
    assert calls["todo"] == [{"entity_id": "todo.alex", "item": "milk"}]


async def test_the_calendar_reads_the_day_in_order(hass: HomeAssistant, rig):
    _, calls = rig
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    speech = await ask(hass, "EchoLocalMyCalendar", day="today")
    assert speech == (
        "You have 3 things today: Holiday all day, Standup at 9:00 AM, Dentist at 2:00 PM."
    )
    assert calls["calendar"][0]["entity_id"] == ["calendar.alex"]


async def test_find_my_phone_rings_loudly(hass: HomeAssistant, rig):
    _, calls = rig
    assert await ask(hass, "EchoLocalFindMyPhone") == "Ringing your phone."
    sent = calls["notify"][0]
    assert sent["data"]["channel"] == "alarm_stream"
    assert sent["data"]["push"]["sound"]["critical"] == 1


async def test_a_reminder_arrives_after_its_wait_and_survives_until_then(hass: HomeAssistant, rig):
    _, calls = rig
    speech = await ask(hass, "EchoLocalRemindMe", task="call mom", minutes=10)
    assert speech == "I'll remind you in 10 minutes."
    assert len(hass.data[personal.DATA]["reminders"]) == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert calls["notify"] == []

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert calls["notify"] == [{"message": "call mom"}]
    assert hass.data[personal.DATA]["reminders"] == []


async def test_hours_are_spoken_as_hours(hass: HomeAssistant, rig):
    _, calls = rig
    assert await ask(hass, "EchoLocalRemindMe", task="stretch", hours=2) == (
        "I'll remind you in 2 hours."
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2, minutes=1))
    await hass.async_block_till_done()
    assert calls["notify"] == [{"message": "stretch"}]


async def test_the_speaker_action_answers_for_a_device(hass: HomeAssistant, rig):
    answer = await hass.services.async_call(
        "echolocal", "speaker", {"device_id": "dev1"}, blocking=True, return_response=True
    )
    assert answer["person"] == "person.alex"
