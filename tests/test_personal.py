import json
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.echolocal import personal, triggers
from custom_components.echolocal.personal import Outcome

NODE = "office_echo"
ALEX = {"match": "known", "name": "Alex", "person": "person.alex"}


@pytest.fixture
async def rig(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch):
    day = dt_util.start_of_local_day()
    calls: dict[str, list[dict[str, Any]]] = {
        "notify": [],
        "todo": [],
        "calendar": [],
        "events": [
            _timed("Dentist", day.replace(hour=14)),
            _timed("Standup", day.replace(hour=9)),
            {"start": day.date().isoformat(), "end": day.date().isoformat(), "summary": "Holiday"},
        ],
    }

    async def ring(call: ServiceCall):
        calls["notify"].append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_alex_phone", ring)

    async def add(call: ServiceCall):
        calls["todo"].append(dict(call.data))

    hass.services.async_register("todo", "add_item", add)

    async def events(call: ServiceCall):
        calls["calendar"].append(dict(call.data))
        return {"calendar.alex": {"events": calls["events"]}}

    hass.services.async_register(
        "calendar", "get_events", events, supports_response=SupportsResponse.ONLY
    )
    monkeypatch.setattr(personal, "phone_service", _phone)

    hass.states.async_set("person.alex", "home", {"friendly_name": "Alex"})
    await personal.async_setup(hass)
    return calls


def _timed(summary: str, start: Any) -> dict[str, str]:
    end = start + timedelta(minutes=30)
    return {"start": start.isoformat(), "end": end.isoformat(), "summary": summary}


def _phone(hass: HomeAssistant, who: personal.Asker) -> str:
    return "mobile_app_alex_phone"


async def ask(hass: HomeAssistant, operation: str, who: Any = ALEX, **values: Any):
    return await personal.answer(hass, "dev1", who, {"operation": operation, **values})


async def test_an_unknown_voice_gets_nothing_personal(hass: HomeAssistant, rig):
    assert await ask(hass, "add_to_list", {"match": "unknown"}, item="milk") == Outcome(
        "unrecognized"
    )
    assert rig["todo"] == []


async def test_a_voice_with_no_person_is_told_so(hass: HomeAssistant, rig):
    assert await ask(hass, "add_to_list", {**ALEX, "person": ""}, item="milk") == Outcome(
        "unlinked"
    )


async def test_an_operation_nobody_handles_is_no_answer(hass: HomeAssistant, rig):
    assert await ask(hass, "dance") is None


async def test_a_missing_setting_names_what_is_missing(hass: HomeAssistant, rig):
    assert await ask(hass, "add_to_list", item="milk") == Outcome("add_to_list.no_list")


async def test_items_go_on_the_speakers_own_list(hass: HomeAssistant, rig):
    hass.data[personal.DATA]["people"]["person.alex"] = {"list": "todo.alex"}
    assert await ask(hass, "add_to_list", item="milk") == Outcome("add_to_list.done")
    assert rig["todo"] == [{"entity_id": "todo.alex", "item": "milk"}]


async def test_the_calendar_hands_the_day_in_order_to_the_device(hass: HomeAssistant, rig, freezer):
    freezer.move_to(dt_util.start_of_local_day().replace(hour=8))
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    assert await ask(hass, "calendar", day="today") == Outcome(
        "calendar.today",
        {
            "Count": 3,
            "Events": [
                {"Summary": "Holiday", "Time": ""},
                {"Summary": "Standup", "Time": "09:00"},
                {"Summary": "Dentist", "Time": "14:00"},
            ],
            "More": 0,
        },
    )
    assert rig["calendar"][0]["entity_id"] == ["calendar.alex"]


async def test_today_reads_only_what_is_still_ahead(hass: HomeAssistant, rig, freezer):
    freezer.move_to(dt_util.start_of_local_day().replace(hour=12))
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    outcome = await ask(hass, "calendar", day="today")
    assert [e["Summary"] for e in outcome.data["Events"]] == ["Holiday", "Dentist"]


async def test_an_event_in_progress_is_still_read(hass: HomeAssistant, rig, freezer):
    freezer.move_to(dt_util.start_of_local_day().replace(hour=9, minute=10))
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    outcome = await ask(hass, "calendar", day="today")
    assert "Standup" in [e["Summary"] for e in outcome.data["Events"]]


async def test_an_event_on_two_calendars_is_read_once(hass: HomeAssistant, rig, freezer):
    freezer.move_to(dt_util.start_of_local_day().replace(hour=8))
    rig["events"].append(_timed("Dentist", dt_util.start_of_local_day().replace(hour=14)))
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    outcome = await ask(hass, "calendar", day="today")
    assert [e["Summary"] for e in outcome.data["Events"]].count("Dentist") == 1


async def test_past_five_the_rest_are_counted(hass: HomeAssistant, rig, freezer):
    day = dt_util.start_of_local_day()
    freezer.move_to(day.replace(hour=8))
    rig["events"][:] = [_timed(f"Meeting {h}", day.replace(hour=h)) for h in range(10, 17)]
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    outcome = await ask(hass, "calendar", day="today")
    assert outcome.data["Count"] == 7
    assert len(outcome.data["Events"]) == 5
    assert outcome.data["More"] == 2


async def test_a_day_with_everything_behind_it_is_empty(hass: HomeAssistant, rig, freezer):
    freezer.move_to(dt_util.start_of_local_day().replace(hour=15))
    rig["events"][:] = rig["events"][:2]
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    assert await ask(hass, "calendar", day="today") == Outcome("calendar.empty_today")


async def test_a_reminder_arrives_after_its_wait_and_survives_until_then(hass: HomeAssistant, rig):
    assert await ask(hass, "remind", task="call mom", minutes="10", hours="") == Outcome(
        "remind.set"
    )
    assert len(hass.data[personal.DATA]["reminders"]) == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert rig["notify"] == []

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert rig["notify"] == [{"message": "call mom"}]
    assert hass.data[personal.DATA]["reminders"] == []


async def test_a_reminder_in_hours_waits_hours(hass: HomeAssistant, rig):
    assert await ask(hass, "remind", task="stretch", hours="2", minutes="") == Outcome("remind.set")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2, minutes=1))
    await hass.async_block_till_done()
    assert rig["notify"] == [{"message": "stretch"}]


async def test_a_reminder_without_a_number_is_not_caught(hass: HomeAssistant, rig):
    assert await ask(hass, "remind", task="stretch", minutes="ten") == Outcome("remind.not_caught")


ARGS = [
    {"name": name, "type": "string"}
    for name in ("operation", "day", "task", "minutes", "hours", "item", "key", "data")
]


def declared(hass: HomeAssistant) -> triggers.Triggers:
    t = triggers.Triggers(hass)
    t.devices["dev1"] = triggers.Declared(
        actions={"personal": {"name": "personal", "args": ARGS}}, at=0
    )
    return t


async def test_the_reply_is_the_devices_own_text(
    hass: HomeAssistant, rig, monkeypatch: pytest.MonkeyPatch
):
    hass.data[personal.DATA]["people"]["person.alex"] = {"list": "todo.alex"}
    monkeypatch.setattr(triggers, "_node", lambda hass, device_id: NODE)
    sent: list[dict[str, Any]] = []

    async def device(call: ServiceCall):
        sent.append(dict(call.data))
        return {
            "answer": {
                "speaker": ALEX,
                "replies": {"add_to_list.done": "Milch steht jetzt auf deiner Liste."},
            }
        }

    hass.services.async_register(
        "esphome", f"{NODE}_personal", device, supports_response=SupportsResponse.ONLY
    )
    reply = await declared(hass)._run(
        "dev1", "personal", {"operation": "add_to_list", "item": "Milch"}, "de"
    )
    assert reply == "Milch steht jetzt auf deiner Liste."
    assert len(sent) == 1
    assert rig["todo"] == [{"entity_id": "todo.alex", "item": "Milch"}]


async def test_the_calendar_is_phrased_by_a_second_call(
    hass: HomeAssistant, rig, monkeypatch: pytest.MonkeyPatch, freezer
):
    freezer.move_to(dt_util.start_of_local_day().replace(hour=8))
    hass.data[personal.DATA]["people"]["person.alex"] = {"calendars": ["calendar.alex"]}
    monkeypatch.setattr(triggers, "_node", lambda hass, device_id: NODE)
    sent: list[dict[str, Any]] = []

    async def device(call: ServiceCall):
        sent.append(dict(call.data))
        if call.data["operation"] == "reply":
            return {"answer": {"reply": "You have 3 things today."}}
        return {"answer": {"speaker": ALEX, "replies": {"failed": "Sorry, that didn't work."}}}

    hass.services.async_register(
        "esphome", f"{NODE}_personal", device, supports_response=SupportsResponse.ONLY
    )
    reply = await declared(hass)._run(
        "dev1", "personal", {"operation": "calendar", "day": "today"}, "en"
    )
    assert reply == "You have 3 things today."
    assert sent[1]["key"] == "calendar.today"
    assert json.loads(sent[1]["data"])["Count"] == 3


async def test_an_outcome_the_device_cannot_phrase_falls_back_to_its_failure_text(
    hass: HomeAssistant, rig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(triggers, "_node", lambda hass, device_id: NODE)

    async def device(call: ServiceCall):
        return {"answer": {"speaker": ALEX, "replies": {"failed": "Sorry, that didn't work."}}}

    hass.services.async_register(
        "esphome", f"{NODE}_personal", device, supports_response=SupportsResponse.ONLY
    )
    reply = await declared(hass)._run("dev1", "personal", {"operation": "dance"}, "en")
    assert reply == "Sorry, that didn't work."


async def test_a_reminder_takes_the_devices_reading_of_a_spoken_number(
    hass: HomeAssistant, rig, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(triggers, "_node", lambda hass, device_id: NODE)

    async def device(call: ServiceCall):
        return {
            "answer": {
                "speaker": ALEX,
                "slots": {"minutes": "10"},
                "replies": {"remind.set": "I'll send a reminder to your phone in 10 minutes."},
            }
        }

    hass.services.async_register(
        "esphome", f"{NODE}_personal", device, supports_response=SupportsResponse.ONLY
    )
    reply = await declared(hass)._run(
        "dev1", "personal", {"operation": "remind", "task": "stretch", "minutes": "ten"}, "en"
    )
    assert reply == "I'll send a reminder to your phone in 10 minutes."
    assert len(hass.data[personal.DATA]["reminders"]) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert rig["notify"] == [{"message": "stretch"}]
