import pytest
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

from custom_components.echolocal import fetch
from custom_components.echolocal.fetch import allowed, total_of


async def test_only_administrators_and_devices_may_call_fetch(
    hass: HomeAssistant, hass_read_only_user: User
):
    fetch.async_setup(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            "echolocal",
            "fetch",
            {"url": "https://github.com/x"},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://github.com/ygelfand/echolocal/releases/download/1.0.0/manifest.json", True),
        ("https://api.github.com/repos/ygelfand/echolocal/releases", True),
        ("https://release-assets.githubusercontent.com/github-production-release-asset/1", True),
        ("https://d.echolocal.link/pryon/1/model.tar.xz", True),
        ("https://files.echolocal.link/x", True),
        ("https://echolocal.link/x", False),
        ("https://evilecholocal.link/x", False),
        ("https://echolocal.link.evil.com/x", False),
        ("https://x.github.com/x", False),
        ("http://github.com/x", False),
        ("https://github.com:8443/x", False),
        ("https://example.com/x", False),
        ("http://homeassistant.local:8123/api/", False),
    ],
)
def test_only_release_hosts_are_allowed(url, ok):
    assert allowed(url) is ok


@pytest.mark.parametrize(
    ("header", "total"),
    [
        ("bytes 0-99/444438", 444438),
        ("bytes 40960-41059/*", 0),
        (None, 0),
        ("", 0),
    ],
)
def test_total_comes_from_content_range(header, total):
    assert total_of(header) == total
