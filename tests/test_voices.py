import json

from homeassistant.core import HomeAssistant

from custom_components.echolocal import voices


def voice(id_: str, name: str, threshold: float = 0.5) -> dict:
    return {"id": id_, "name": name, "person": "", "prints": [[1.0, 0.0]], "threshold": threshold}


async def test_changes_apply_in_arrival_order_and_the_list_comes_back(hass: HomeAssistant):
    await voices.async_setup(hass)
    changes = [
        {"save": voice("person.alex", "Alex")},
        {"save": voice("person.beth", "Beth")},
        {"save": voice("person.alex", "Alex", 0.6)},
        {"remove": "person.beth"},
    ]
    answer = await hass.services.async_call(
        "echolocal",
        "voices",
        {"changes": json.dumps(changes)},
        blocking=True,
        return_response=True,
    )
    assert [(p["id"], p["threshold"]) for p in answer["profiles"]] == [("person.alex", 0.6)]


async def test_an_invalid_voice_is_skipped(hass: HomeAssistant):
    await voices.async_setup(hass)
    bad = [
        {"save": voice("a", "A", 0.2)},
        {"save": {**voice("b", "B"), "prints": []}},
        {"save": {"id": "c"}},
        "nonsense",
    ]
    assert not await voices._apply(hass, bad)
    assert hass.data[voices.DATA]["profiles"] == []


async def test_an_unchanged_voice_changes_nothing(hass: HomeAssistant):
    await voices.async_setup(hass)
    assert await voices._apply(hass, [{"save": voice("a", "A")}])
    assert not await voices._apply(hass, [{"save": voice("a", "A")}])


async def test_a_change_tells_the_panel(hass: HomeAssistant):
    await voices.async_setup(hass)
    told = []
    hass.bus.async_listen(voices.UPDATED, told.append)
    await voices._apply(hass, [{"save": voice("a", "A")}])
    await voices._apply(hass, [{"save": voice("a", "A")}])
    await hass.async_block_till_done()
    assert len(told) == 1


async def test_null_changes_are_no_changes(hass: HomeAssistant):
    await voices.async_setup(hass)
    answer = await hass.services.async_call(
        "echolocal", "voices", {"changes": "null"}, blocking=True, return_response=True
    )
    assert answer == {"profiles": []}


def enrolled(id_: str) -> dict:
    return {**voice(id_, id_), "prints": [[0.123456 + i] * 192 for i in range(5)]}


async def test_a_paged_answer_stays_within_a_page_and_covers_everyone(hass: HomeAssistant):
    await voices.async_setup(hass)
    names = [f"v{i:02d}" for i in range(12)]
    await voices._apply(hass, [{"save": enrolled(n)} for n in names])

    got, page, asked = [], 0, 0
    while page is not None:
        answer = await hass.services.async_call(
            "echolocal", "voices", {"page": str(page)}, blocking=True, return_response=True
        )
        assert len(json.dumps(answer["profiles"])) <= voices.PAGE_BYTES
        got += [p["id"] for p in answer["profiles"]]
        page = answer.get("next")
        asked += 1

    assert got == names
    assert asked > 1


async def test_a_page_past_the_end_is_empty(hass: HomeAssistant):
    await voices.async_setup(hass)
    await voices._apply(hass, [{"save": voice("a", "Ann")}])
    answer = await hass.services.async_call(
        "echolocal", "voices", {"page": "5"}, blocking=True, return_response=True
    )
    assert answer["profiles"] == []
    assert "next" not in answer


async def test_a_change_moves_the_revision(hass: HomeAssistant):
    await voices.async_setup(hass)
    before = hass.data[voices.DATA]["revision"]
    await voices._apply(hass, [{"save": voice("a", "Ann")}])
    await voices._apply(hass, [{"save": voice("a", "Ann")}])
    assert hass.data[voices.DATA]["revision"] == before + 1


async def test_a_device_with_voice_sync_is_nudged_not_sent_the_list(hass: HomeAssistant):
    await voices.async_setup(hass)
    called = []

    async def record(call):
        called.append((call.service, dict(call.data)))

    hass.services.async_register("esphome", "kitchen_voice_sync", record)
    hass.services.async_register("esphome", "kitchen_voice_profiles", record)

    await voices._send(hass, "kitchen")

    assert called == [("kitchen_voice_sync", {})]


async def test_asking_with_no_changes_returns_the_household(hass: HomeAssistant):
    await voices.async_setup(hass)
    await voices._apply(hass, [{"save": voice("a", "Ann")}])
    answer = await hass.services.async_call(
        "echolocal", "voices", {}, blocking=True, return_response=True
    )
    assert [p["name"] for p in answer["profiles"]] == ["Ann"]
