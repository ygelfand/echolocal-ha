from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import probatio
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.esphome import DOMAIN as ESPHOME_DOMAIN
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    intent,
)
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN
from .fleet import _devices

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}.people"
STORE_VERSION = 1
DATA = "echolocal_people"

DEVICE_ACTION = "turn_speaker"
WAIT = 5
MOBILE_APP = "mobile_app"
MOST_EVENTS = 5

KNOWN = "known"
PENDING = "pending"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    selector: dict[str, Any]


PHONE = Field("phone", "Phone", {"device": {"integration": MOBILE_APP}})
CALENDARS = Field("calendars", "Calendars", {"entity": {"domain": "calendar", "multiple": True}})
LIST = Field("list", "To-do list", {"entity": {"domain": "todo"}})
FIELDS = (PHONE, CALENDARS, LIST)


@dataclass(frozen=True)
class Asker:
    person: str
    name: str
    record: dict[str, Any]


class Refusal(Exception):
    def __init__(self, speech: str) -> None:
        super().__init__(speech)
        self.speech = speech


Handle = Callable[[HomeAssistant, Asker, dict[str, Any], str | None], Awaitable[str]]


@dataclass(frozen=True)
class Personal:
    intent_type: str
    description: str
    sentences: list[tuple[list[str], dict[str, Any]]]
    slots: dict[Any, Any]
    handle: Handle
    needs: tuple[Field, ...] = field(default=())


async def async_setup(hass: HomeAssistant) -> None:
    store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
    saved = await store.async_load() or {}
    hass.data[DATA] = {
        "store": store,
        "people": saved.get("people", {}),
        "reminders": saved.get("reminders", []),
        "timers": {},
    }
    for reminder in list(hass.data[DATA]["reminders"]):
        _schedule(hass, reminder)

    for action in ACTIONS:
        intent.async_register(hass, PersonalHandler(action))
    _register_sentences(hass)

    async def who(call: ServiceCall) -> ServiceResponse:
        return await speaker(hass, call.data["device_id"])

    hass.services.async_register(
        DOMAIN,
        "speaker",
        who,
        schema=vol.Schema({vol.Required("device_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    websocket_api.async_register_command(hass, ws_people)
    websocket_api.async_register_command(hass, ws_save)


async def _save(hass: HomeAssistant) -> None:
    data = hass.data[DATA]
    await data["store"].async_save({"people": data["people"], "reminders": data["reminders"]})


def _node(hass: HomeAssistant, device_id: str) -> str | None:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    top = device.via_device_id or device.id
    return next((node for d, node, _ in _devices(hass) if d.id == top), None)


async def speaker(hass: HomeAssistant, device_id: str | None) -> dict[str, Any]:
    node = _node(hass, device_id) if device_id else None
    if node is None or not hass.services.has_service(ESPHOME_DOMAIN, f"{node}_{DEVICE_ACTION}"):
        return {"match": "none"}
    try:
        async with asyncio.timeout(WAIT):
            answer = await hass.services.async_call(
                ESPHOME_DOMAIN,
                f"{node}_{DEVICE_ACTION}",
                {},
                blocking=True,
                return_response=True,
            )
    except (HomeAssistantError, TimeoutError) as err:
        _LOGGER.warning("asking %s who is speaking failed: %s", node, err)
        return {"match": "none"}
    return dict(answer or {"match": "none"})


def _name(hass: HomeAssistant, person: str) -> str:
    state = hass.states.get(person)
    return state.name if state is not None else person


async def asker(hass: HomeAssistant, device_id: str | None) -> Asker:
    who = await speaker(hass, device_id)
    match = who.get("match")
    if match == PENDING:
        _LOGGER.info("voice match had not landed when a personal action asked")
    if match != KNOWN:
        raise Refusal("I don't know whose voice that is.")
    person = who.get("person") or ""
    if not person:
        raise Refusal(f"{who.get('name') or 'Sorry'}, your voice isn't linked to a person yet.")
    return Asker(person, _name(hass, person), hass.data[DATA]["people"].get(person, {}))


def default_phone(hass: HomeAssistant, person: str) -> str | None:
    state = hass.states.get(person)
    if state is None:
        return None
    registry = er.async_get(hass)
    for tracker in state.attributes.get("device_trackers", []):
        entry = registry.async_get(tracker)
        if entry is not None and entry.platform == MOBILE_APP and entry.device_id:
            return entry.device_id
    return None


def phone_service(hass: HomeAssistant, asker_: Asker) -> str:
    device_id = asker_.record.get(PHONE.name) or default_phone(hass, asker_.person)
    device = dr.async_get(hass).async_get(device_id) if device_id else None
    if device is None:
        raise Refusal(f"{asker_.name}, there's no phone set up for you.")
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == MOBILE_APP:
            service = slugify(f"{MOBILE_APP}_{entry.data['device_name']}")
            if hass.services.has_service("notify", service):
                return service
    raise Refusal(f"{asker_.name}, your phone can't take notifications.")


async def notify(hass: HomeAssistant, service: str, message: str, **data: Any) -> None:
    payload: dict[str, Any] = {"message": message}
    if data:
        payload["data"] = data
    await hass.services.async_call("notify", service, payload, blocking=True)


async def _calendar(hass: HomeAssistant, who: Asker, slots: dict[str, Any], _: str | None) -> str:
    calendars = who.record.get(CALENDARS.name) or []
    if not calendars:
        raise Refusal(f"{who.name}, there's no calendar set up for you.")
    day = slots.get("day") or "today"
    start = dt_util.start_of_local_day() + timedelta(days=1 if day == "tomorrow" else 0)
    answer = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": calendars,
            "start_date_time": start,
            "end_date_time": start + timedelta(days=1),
        },
        blocking=True,
        return_response=True,
    )
    events = [e for found in (answer or {}).values() for e in found.get("events", [])]
    if not events:
        return f"Nothing on your calendar {day}."
    events.sort(key=lambda e: e["start"])
    said = [_event(e) for e in events[:MOST_EVENTS]]
    count = len(events)
    lead = f"You have {count} thing{'s' if count != 1 else ''} {day}"
    return f"{lead}: {', '.join(said)}."


def _event(event: dict[str, Any]) -> str:
    start = event["start"]
    if "T" not in start:
        return f"{event['summary']} all day"
    when = dt_util.as_local(datetime.fromisoformat(start))
    return f"{event['summary']} at {when.strftime('%-I:%M %p')}"


async def _ring(hass: HomeAssistant, who: Asker, _: dict[str, Any], __: str | None) -> str:
    service = phone_service(hass, who)
    await notify(
        hass,
        service,
        "Find my phone",
        ttl=0,
        priority="high",
        channel="alarm_stream",
        push={"sound": {"name": "default", "critical": 1, "volume": 1.0}},
    )
    return "Ringing your phone."


async def _send(hass: HomeAssistant, who: Asker, _: dict[str, Any], device_id: str | None) -> str:
    said = _last_reply(hass, device_id)
    if not said:
        return "There's nothing to send."
    await notify(hass, phone_service(hass, who), said)
    return "Sent to your phone."


def _last_reply(hass: HomeAssistant, device_id: str | None) -> str | None:
    if not device_id:
        return None
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry in er.async_entries_for_device(er.async_get(hass), device.via_device_id or device.id):
        if entry.platform == ESPHOME_DOMAIN and entry.unique_id.endswith("-last_reply"):
            state = hass.states.get(entry.entity_id)
            if state is not None and state.state not in ("unknown", "unavailable", ""):
                return state.state
    return None


async def _remind(hass: HomeAssistant, who: Asker, slots: dict[str, Any], _: str | None) -> str:
    phone_service(hass, who)
    minutes = _minutes(slots)
    if minutes is None:
        return "I didn't catch when."
    reminder = {
        "id": uuid.uuid4().hex,
        "person": who.person,
        "task": slots["task"],
        "at": (dt_util.utcnow() + timedelta(minutes=minutes)).isoformat(),
    }
    hass.data[DATA]["reminders"].append(reminder)
    await _save(hass)
    _schedule(hass, reminder)
    return f"I'll remind you in {_span(minutes)}."


def _minutes(slots: dict[str, Any]) -> int | None:
    try:
        if slots.get("hours") is not None:
            return int(slots["hours"]) * 60
        if slots.get("minutes") is not None:
            return int(slots["minutes"])
    except (TypeError, ValueError):
        return None
    return None


def _span(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _schedule(hass: HomeAssistant, reminder: dict[str, Any]) -> None:
    at = dt_util.parse_datetime(reminder["at"]) or dt_util.utcnow()

    async def due(_: datetime) -> None:
        data = hass.data[DATA]
        data["timers"].pop(reminder["id"], None)
        data["reminders"] = [r for r in data["reminders"] if r["id"] != reminder["id"]]
        await _save(hass)
        record = data["people"].get(reminder["person"], {})
        try:
            service = phone_service(hass, Asker(reminder["person"], "", record))
        except Refusal:
            _LOGGER.warning("no phone to remind %s", reminder["person"])
            return
        await notify(hass, service, reminder["task"])

    hass.data[DATA]["timers"][reminder["id"]] = async_track_point_in_utc_time(
        hass, due, max(at, dt_util.utcnow())
    )


async def _add(hass: HomeAssistant, who: Asker, slots: dict[str, Any], _: str | None) -> str:
    todo = who.record.get(LIST.name)
    if not todo:
        raise Refusal(f"{who.name}, there's no list set up for you.")
    item = slots["item"]
    await hass.services.async_call(
        "todo", "add_item", {"entity_id": todo, "item": item}, blocking=True
    )
    return f"Added {item} to your list."


ACTIONS = (
    Personal(
        "EchoLocalMyCalendar",
        "Read the speaker's own calendar for today or tomorrow.",
        [
            (["(what's|what is) on my calendar [today]", "what do I have today"], {"day": "today"}),
            (
                ["(what's|what is) on my calendar tomorrow", "what do I have tomorrow"],
                {"day": "tomorrow"},
            ),
        ],
        {probatio.Optional("day"): probatio.In(["today", "tomorrow"])},
        _calendar,
        (CALENDARS,),
    ),
    Personal(
        "EchoLocalFindMyPhone",
        "Ring the speaker's own phone.",
        [(["find my phone", "where's my phone", "call me", "ring my phone"], {})],
        {},
        _ring,
        (PHONE,),
    ),
    Personal(
        "EchoLocalSendToMyPhone",
        "Send this device's last answer to the speaker's own phone.",
        [(["send (that|it) to my phone"], {})],
        {},
        _send,
        (PHONE,),
    ),
    Personal(
        "EchoLocalRemindMe",
        "Remind the speaker on their own phone after some minutes.",
        [
            (["remind me to {task} in {minutes} minute[s]"], {}),
            (["remind me to {task} in {hours} hour[s]"], {}),
        ],
        {
            probatio.Required("task"): intent.non_empty_string,
            probatio.Optional("minutes"): probatio.Coerce(int),
            probatio.Optional("hours"): probatio.Coerce(int),
        },
        _remind,
        (PHONE,),
    ),
    Personal(
        "EchoLocalAddToMyList",
        "Add an item to the speaker's own to-do list.",
        [(["add {item} to my [to-do|todo|shopping] list"], {})],
        {probatio.Required("item"): intent.non_empty_string},
        _add,
        (LIST,),
    ),
)


class PersonalHandler(intent.IntentHandler):
    def __init__(self, action: Personal) -> None:
        self.action = action
        self.intent_type = action.intent_type
        self.description = action.description

    @property
    def slot_schema(self) -> dict[Any, Any]:
        return self.action.slots

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass = intent_obj.hass
        slots = {name: slot.get("value") for name, slot in intent_obj.slots.items()}
        device_id = intent_obj.device_id
        try:
            who = await asker(hass, device_id)
            speech = await self.action.handle(hass, who, slots, device_id)
        except Refusal as refusal:
            speech = refusal.speech
        response = intent_obj.create_response()
        response.async_set_speech(speech)
        return response


def _register_sentences(hass: HomeAssistant) -> None:
    from homeassistant.components.conversation.agent_manager import get_agent_manager

    manager = get_agent_manager(hass)
    for action in ACTIONS:
        for sentences, fixed in action.sentences:
            manager.register_trigger(sentences, _answer(hass, action.intent_type, fixed))


def _answer(hass: HomeAssistant, intent_type: str, fixed: dict[str, Any]):
    async def answer(user_input: Any, result: Any) -> str | None:
        slots = {**fixed, **{name: e.value for name, e in result.entities.items()}}
        device_id = user_input.device_id
        if user_input.satellite_id is not None:
            entry = er.async_get(hass).async_get(user_input.satellite_id)
            if entry is not None:
                device_id = entry.device_id
        try:
            response = await intent.async_handle(
                hass,
                DOMAIN,
                intent_type,
                {name: {"value": value} for name, value in slots.items()},
                user_input.text,
                user_input.context,
                user_input.language,
                device_id=device_id,
                satellite_id=user_input.satellite_id,
            )
        except intent.InvalidSlotInfo:
            return "I didn't catch that."
        return response.speech.get("plain", {}).get("speech")

    return answer


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/people"})
@websocket_api.async_response
async def ws_people(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    records = hass.data[DATA]["people"]
    people = [
        {
            "person": state.entity_id,
            "name": state.name,
            "record": records.get(state.entity_id, {}),
            "phone": default_phone(hass, state.entity_id),
        }
        for state in sorted(hass.states.async_all("person"), key=lambda s: s.name.lower())
    ]
    fields = [{"name": f.name, "label": f.label, "selector": f.selector} for f in FIELDS]
    connection.send_result(msg["id"], {"fields": fields, "people": people})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/people/save",
        vol.Required("person"): vol.Match(r"^person\.[a-z0-9_]+$"),
        vol.Required("record"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    names = {f.name for f in FIELDS}
    record = {k: v for k, v in msg["record"].items() if k in names and v not in (None, "", [])}
    people = hass.data[DATA]["people"]
    if record:
        people[msg["person"]] = record
    else:
        people.pop(msg["person"], None)
    await _save(hass)
    connection.send_result(msg["id"], {})
