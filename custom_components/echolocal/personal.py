from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}.people"
STORE_VERSION = 1
DATA = "echolocal_people"

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


@dataclass(frozen=True)
class Outcome:
    key: str
    data: dict[str, Any] | None = None


class Refusal(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


Handle = Callable[[HomeAssistant, Asker, dict[str, Any], str | None], Awaitable[Outcome]]


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

    websocket_api.async_register_command(hass, ws_people)
    websocket_api.async_register_command(hass, ws_save)


async def _save(hass: HomeAssistant) -> None:
    data = hass.data[DATA]
    await data["store"].async_save({"people": data["people"], "reminders": data["reminders"]})


def _name(hass: HomeAssistant, person: str) -> str:
    state = hass.states.get(person)
    return state.name if state is not None else person


def asker(hass: HomeAssistant, who: dict[str, Any]) -> Asker:
    match = who.get("match")
    if match == PENDING:
        _LOGGER.info("voice match had not landed when a personal action asked")
    if match != KNOWN:
        raise Refusal("unrecognized")
    person = who.get("person") or ""
    if not person:
        raise Refusal("unlinked")
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
        raise Refusal("no_phone")
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == MOBILE_APP:
            service = slugify(f"{MOBILE_APP}_{entry.data['device_name']}")
            if hass.services.has_service("notify", service):
                return service
    raise Refusal("no_notify")


async def notify(hass: HomeAssistant, service: str, message: str, **data: Any) -> None:
    payload: dict[str, Any] = {"message": message}
    if data:
        payload["data"] = data
    await hass.services.async_call("notify", service, payload, blocking=True)


async def _calendar(
    hass: HomeAssistant, who: Asker, slots: dict[str, Any], _: str | None
) -> Outcome:
    calendars = who.record.get(CALENDARS.name) or []
    if not calendars:
        raise Refusal("calendar.none")
    day = "tomorrow" if slots.get("day") == "tomorrow" else "today"
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
    found = [e for listed in (answer or {}).values() for e in listed.get("events", [])]
    events = list({(e["summary"], e["start"]): e for e in found}.values())
    if day == "today":
        now = dt_util.now()
        events = [e for e in events if _ahead(e, now)]
    if not events:
        return Outcome(f"calendar.empty_{day}")
    events.sort(key=lambda e: e["start"])
    said = [_event(e) for e in events[:MOST_EVENTS]]
    return Outcome(
        f"calendar.{day}",
        {"Count": len(events), "Events": said, "More": max(0, len(events) - MOST_EVENTS)},
    )


def _ahead(event: dict[str, Any], now: datetime) -> bool:
    if "T" not in event["start"]:
        return True
    end = event.get("end") or event["start"]
    return dt_util.as_local(datetime.fromisoformat(end)) > now


def _event(event: dict[str, Any]) -> dict[str, str]:
    start = event["start"]
    if "T" not in start:
        return {"Summary": event["summary"], "Time": ""}
    when = dt_util.as_local(datetime.fromisoformat(start))
    return {"Summary": event["summary"], "Time": when.strftime("%H:%M")}


async def _remind(hass: HomeAssistant, who: Asker, slots: dict[str, Any], _: str | None) -> Outcome:
    phone_service(hass, who)
    minutes = _minutes(slots)
    task = slots.get("task")
    if minutes is None or not task:
        return Outcome("remind.not_caught")
    reminder = {
        "id": uuid.uuid4().hex,
        "person": who.person,
        "task": task,
        "at": (dt_util.utcnow() + timedelta(minutes=minutes)).isoformat(),
    }
    hass.data[DATA]["reminders"].append(reminder)
    await _save(hass)
    _schedule(hass, reminder)
    return Outcome("remind.set")


def _minutes(slots: dict[str, Any]) -> int | None:
    try:
        if slots.get("hours") is not None:
            return int(slots["hours"]) * 60
        if slots.get("minutes") is not None:
            return int(slots["minutes"])
    except (TypeError, ValueError):
        return None
    return None


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


async def _add(hass: HomeAssistant, who: Asker, slots: dict[str, Any], _: str | None) -> Outcome:
    todo = who.record.get(LIST.name)
    if not todo:
        raise Refusal("add_to_list.no_list")
    item = slots.get("item")
    if not item:
        return Outcome("add_to_list.not_caught")
    await hass.services.async_call(
        "todo", "add_item", {"entity_id": todo, "item": item}, blocking=True
    )
    return Outcome("add_to_list.done")


ACTION = "personal"
REPLY = "reply"
FAILED = "failed"

OPERATIONS: dict[str, Handle] = {
    "calendar": _calendar,
    "remind": _remind,
    "add_to_list": _add,
}


async def answer(
    hass: HomeAssistant, device_id: str | None, who: Any, values: dict[str, Any]
) -> Outcome | None:
    handle = OPERATIONS.get(str(values.get("operation", "")))
    if handle is None or not isinstance(who, dict):
        return None
    slots = {k: v for k, v in values.items() if v not in (None, "")}
    try:
        return await handle(hass, asker(hass, who), slots, device_id)
    except Refusal as refusal:
        return Outcome(refusal.key)
    except HomeAssistantError as err:
        _LOGGER.warning("personal %s failed: %s", values.get("operation"), err)
        return Outcome(FAILED)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/people"})
@websocket_api.require_admin
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
