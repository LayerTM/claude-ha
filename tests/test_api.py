"""Tests for the add-on HTTP client (contract mapping)."""

from __future__ import annotations

from aiohttp import ClientError
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import (
    ChatHealth,
    ClaudeAuthError,
    ClaudeClient,
    ClaudeConnectionError,
    ClaudeError,
    ClaudeRateLimitError,
    ClaudeRequestError,
    _epoch_ms,
    _non_negative_int,
)
from custom_components.claude_ha.const import HEADER_CALLER, MODE_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .conftest import STATUS_PAYLOAD, TEST_BASE_URL, TEST_TOKEN


def _client(hass: HomeAssistant) -> ClaudeClient:
    return ClaudeClient(async_get_clientsession(hass), TEST_BASE_URL, TEST_TOKEN)


@pytest.mark.parametrize(
    ("version", "sent"),
    [
        (None, False),  # no status seen yet -> never send
        ("not-a-version", False),  # unparseable -> treat as unsupported
        ("1.27.9", False),  # below the gate
        ("1.28.0", True),  # exactly the gate
        ("1.29.0", True),  # above the gate
    ],
)
async def test_surface_gated_on_addon_version(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    version: str | None,
    sent: bool,
) -> None:
    """`surface` is only put on the wire for add-ons >= 1.28.0 (else dropped)."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    client = _client(hass)
    client.note_version(version)

    await client.async_prompt("hi", surface="voice")

    body = aioclient_mock.mock_calls[-1][2]
    assert ("surface" in body) is sent
    if sent:
        assert body["surface"] == "voice"


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        (None, False),
        ("not-a-version", False),
        ("1.35.9", False),
        ("1.36.0", True),
        ("1.40.0", True),
    ],
)
async def test_supports_edit_automation_version_gate(
    hass: HomeAssistant, version: str | None, supported: bool
) -> None:
    """`edit_automation` is only offered to add-ons >= 1.36.0."""
    client = _client(hass)
    client.note_version(version)
    assert client.supports_edit_automation is supported


async def test_status_parsing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A healthy status response parses into a StatusResult."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json=STATUS_PAYLOAD)
    status = await _client(hass).async_get_status()
    assert status.ready is True
    assert status.model == "claude-sonnet-4-6"
    assert status.ha_mcp_connected is True


async def test_status_parses_chat_health(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A chat_health object (add-on >= 1.20.0) parses into ChatHealth."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 3,
                "degraded": 1,
                "recovered": 2,
                "last_reason": "no-result",
            },
        },
    )
    status = await _client(hass).async_get_status()
    assert status.chat_health is not None
    assert status.chat_health.recent == 3
    assert status.chat_health.degraded == 1
    assert status.chat_health.recovered == 2
    assert status.chat_health.last_reason == "no-result"
    assert status.chat_health.last_failure_ts is None
    assert status.chat_health.window_from_ts is None
    assert status.chat_health.window_to_ts is None


async def test_status_parses_chat_health_timestamps(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The 1.49.0 stamps parse; a null one stays unknown rather than becoming 0."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 38,
                "degraded": 1,
                "recovered": 1,
                "last_reason": "model-error",
                "last_failure_ts": 1757000000000,
                "window_from_ts": None,
                "window_to_ts": 1757100000000,
            },
        },
    )
    status = await _client(hass).async_get_status()
    assert status.chat_health is not None
    assert status.chat_health.last_failure_ts == 1757000000000
    assert status.chat_health.window_from_ts is None
    assert status.chat_health.window_to_ts == 1757100000000


@pytest.mark.parametrize(
    "raw",
    [
        0,
        -1,
        "1757000000000",
        True,
        # The (0, 1) family: these pass a raw `> 0` test and then truncate to
        # exactly the 0 being guarded against, dating the failure to 1970 and
        # clearing the warning. The guard must run on the truncated value.
        0.5,
        0.999,
        1e-9,
    ],
)
async def test_status_chat_health_rejects_non_timestamps(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, raw: object
) -> None:
    """Anything that isn't a positive number reads as unknown, not as 1970.

    A 0 taken at face value would date the failure to 1970, make it stale, and
    silently clear the warning — the one direction this must never fail in.
    """
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 4,
                "degraded": 4,
                "recovered": 0,
                "last_reason": "model-error",
                "last_failure_ts": raw,
            },
        },
    )
    status = await _client(hass).async_get_status()
    assert status.chat_health is not None
    assert status.chat_health.last_failure_ts is None
    assert status.chat_health.is_failure_stale(dt_util.utcnow()) is False


@pytest.mark.parametrize("raw", [float("inf"), float("-inf"), float("nan")])
def test_parsers_survive_non_finite_numbers(raw: float) -> None:
    """A non-finite number reads as unknown instead of raising out of the parser.

    ``int(inf)`` raises ``OverflowError``, which is not a ``ClaudeError``, so it
    would escape the coordinator's handler and take every status entity
    unavailable on each poll. Reachable because aiohttp decodes with stdlib
    ``json``, which accepts ``Infinity`` and ``NaN``.

    Tested against the parser directly, not over the wire: the aiohttp test double
    decodes with HA's orjson-backed loader, which REJECTS those tokens, so a
    wire-level test here would be measuring the mock rather than the code.
    """
    assert _epoch_ms(raw) is None
    assert _non_negative_int(raw) is None


def test_chat_health_failure_rate_empty_window() -> None:
    """An empty window divides by nothing and reads as no failures."""
    health = ChatHealth(recent=0, degraded=0, recovered=0, last_reason=None)
    assert health.failure_rate == 0.0
    assert health.has_recovered is False


@pytest.mark.parametrize(
    ("recent", "degraded", "consecutive_ok", "expected"),
    [
        # Every success came after the last failure — the failures are behind us.
        (3, 1, 2, True),
        (10, 2, 8, True),
        # A success sits BEFORE the last failure, so the window still flaps.
        (12, 3, 3, False),
        (10, 2, 5, False),
        # The whole window failed: 0 successes, 0 since — never recovery, even
        # though `0 >= 0` holds.
        (4, 4, 0, False),
        # Newest run failed.
        (10, 2, 0, False),
        # An add-on older than 1.49.0 reports nothing and gets no rescue.
        (3, 1, None, False),
    ],
)
def test_chat_health_has_recovered(
    recent: int, degraded: int, consecutive_ok: int | None, expected: bool
) -> None:
    """Recovery is every success falling after the last failure — never a count."""
    health = ChatHealth(
        recent=recent,
        degraded=degraded,
        recovered=0,
        last_reason=None,
        consecutive_ok=consecutive_ok,
    )
    assert health.has_recovered is expected


@pytest.mark.parametrize("raw", [-1, "2", 1.5, True, None])
async def test_status_chat_health_consecutive_ok_coercion(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, raw: object
) -> None:
    """Only a non-negative number counts; anything else withholds the rescue.

    ``1.5`` is the one value that survives, as ``1`` — a float is still a number,
    and truncating down is the conservative direction.
    """
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "chat_health": {
                "recent": 3,
                "degraded": 1,
                "recovered": 0,
                "last_reason": "model-error",
                "consecutive_ok": raw,
            },
        },
    )
    status = await _client(hass).async_get_status()
    assert status.chat_health is not None
    assert status.chat_health.consecutive_ok == (1 if raw == 1.5 else None)


async def test_status_chat_health_absent_is_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An older add-on that omits chat_health yields None (backward-compatible)."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json={"ready": True})
    status = await _client(hass).async_get_status()
    assert status.chat_health is None


async def test_status_parses_timeout_and_budget(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """prompt_timeout_ms and budget (add-on >= 1.21.0) parse into the status."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "prompt_timeout_ms": 120000,
            "budget": {"limit": 5.0, "spent": 1.5},
        },
    )
    status = await _client(hass).async_get_status()
    assert status.prompt_timeout_ms == 120000
    assert status.budget is not None
    assert status.budget.limit == 5.0
    assert status.budget.spent == 1.5


async def test_status_timeout_and_budget_absent_are_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Older add-ons omit both fields → None (backward-compatible)."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json={"ready": True})
    status = await _client(hass).async_get_status()
    assert status.prompt_timeout_ms is None
    assert status.budget is None


async def test_status_parses_alerts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An alerts object (add-on >= 1.39.0) parses into Alerts with its items."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "alerts": {
                "active": 2,
                "critical": 1,
                "items": [
                    {
                        "key": "offline:device_tracker.ucg_fiber",
                        "critical": True,
                        "line": "Offline: UCG Fiber",
                    },
                    {
                        "key": "co2:sensor.bedroom_co2",
                        "critical": False,
                        "line": "High CO2: Bedroom CO2 (1850 ppm)",
                    },
                ],
            },
        },
    )
    status = await _client(hass).async_get_status()
    assert status.alerts is not None
    assert status.alerts.active == 2
    assert status.alerts.critical == 1
    assert len(status.alerts.items) == 2
    assert status.alerts.items[0].key == "offline:device_tracker.ucg_fiber"
    assert status.alerts.items[0].critical is True
    assert status.alerts.items[0].line == "Offline: UCG Fiber"
    assert status.alerts.items[1].critical is False


@pytest.mark.parametrize("alerts", [None, "not-a-dict", 42])
async def test_status_alerts_absent_or_null_is_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, alerts: object
) -> None:
    """A missing field, an explicit null, or a non-dict all yield alerts=None.

    An older add-on omits the key; a v1.39.0 add-on with proactive alerts off (or
    not yet ticked) reports ``null`` — both must leave the sensor unavailable.
    """
    body: dict[str, object] = {"ready": True}
    if alerts is not None:
        body["alerts"] = alerts
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json=body)
    status = await _client(hass).async_get_status()
    assert status.alerts is None


async def test_status_alerts_skips_malformed_items(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Non-dict items are skipped and missing item keys default, never raising."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={
            "ready": True,
            "alerts": {
                "active": 1,
                "critical": 0,
                "items": ["garbage", {"key": "battery:sensor.x"}],
            },
        },
    )
    status = await _client(hass).async_get_status()
    assert status.alerts is not None
    assert len(status.alerts.items) == 1  # the "garbage" string was skipped
    assert status.alerts.items[0].key == "battery:sensor.x"
    assert status.alerts.items[0].critical is False  # missing → default
    assert status.alerts.items[0].line == ""  # missing → default


async def test_note_prompt_timeout_tracks_addon_budget(hass: HomeAssistant) -> None:
    """The read timeout stays a margin above the add-on budget, never below floor."""
    client = _client(hass)
    assert client.read_timeout == 135.0  # floor
    client.note_prompt_timeout(200000)  # 200s + 15s margin
    assert client.read_timeout == 215.0
    client.note_prompt_timeout(60000)  # 60s + 15 < floor → floor
    assert client.read_timeout == 135.0
    client.note_prompt_timeout(None)  # no report → floor
    assert client.read_timeout == 135.0


async def test_prompt_sends_headers_and_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Prompt requests carry the bearer token, caller header, mode and id."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={
            "text": "hi",
            "proposal": {"summary": "s", "intents": [{"targets": ["light.x"]}]},
            "tools_used": ["t"],
            "truncated": True,
        },
    )
    intents = [{"intent": "HassTurnOff", "targets": ["light.x"], "data": {}}]
    result = await _client(hass).async_prompt(
        "hello",
        mode=MODE_WRITE,
        conversation_id="conv-1",
        caller="user-1",
        intents=intents,
    )
    assert result.text == "hi"
    assert result.proposal is not None
    assert result.proposal.summary == "s"
    assert result.truncated is True

    _method, _url, body, headers = aioclient_mock.mock_calls[-1]
    assert body == {
        "prompt": "hello",
        "mode": MODE_WRITE,
        "conversation_id": "conv-1",
        "intents": intents,
    }
    assert headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert headers[HEADER_CALLER] == "user-1"


async def test_prompt_read_mode_omits_intents(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Read requests never carry an intents field (contract §2)."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await _client(hass).async_prompt("hello")
    body = aioclient_mock.mock_calls[-1][2]
    assert "intents" not in body
    assert body["mode"] == "read"


async def test_prompt_sends_language_when_given(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The HA conversation language is forwarded so the add-on can localize."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await _client(hass).async_prompt("привіт", language="uk")
    assert aioclient_mock.mock_calls[-1][2]["language"] == "uk"


async def test_prompt_omits_language_when_absent(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """No language means no field — additive, backward-compatible with old add-ons."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await _client(hass).async_prompt("hello")
    assert "language" not in aioclient_mock.mock_calls[-1][2]


async def test_prompt_parses_automation_draft(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A drafted automation (add-on >= 1.34.0) is carried on the PromptResult."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={
            "text": "drafted",
            "proposal": None,
            "automation": {"alias": "A", "triggers": [{}], "actions": [{}]},
            "tools_used": [],
            "truncated": False,
        },
    )
    result = await _client(hass).async_prompt("make an automation")
    assert result.automation is not None
    assert result.automation["alias"] == "A"


@pytest.mark.parametrize("automation", [None, "not-a-dict", 42])
async def test_prompt_automation_absent_or_malformed_is_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, automation: object
) -> None:
    """No draft (absent) or a non-dict value both yield automation=None."""
    body: dict[str, object] = {
        "text": "ok",
        "proposal": None,
        "tools_used": [],
        "truncated": False,
    }
    if automation is not None:
        body["automation"] = automation
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=body)
    result = await _client(hass).async_prompt("hello")
    assert result.automation is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ClaudeAuthError),
        (403, ClaudeAuthError),
        (413, ClaudeRequestError),
        (400, ClaudeRequestError),
        (429, ClaudeRateLimitError),
        (503, ClaudeRateLimitError),
        (504, ClaudeConnectionError),
        (502, ClaudeConnectionError),
        (500, ClaudeError),
    ],
)
async def test_status_code_mapping(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    expected: type[ClaudeError],
) -> None:
    """Each documented HTTP status maps to the right typed error."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", status=status)
    with pytest.raises(expected):
        await _client(hass).async_get_status()


async def test_transport_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transport failure maps to a connection error."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", exc=ClientError())
    with pytest.raises(ClaudeConnectionError):
        await _client(hass).async_get_status()


async def test_timeout(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A timeout maps to a connection error."""
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", exc=TimeoutError())
    with pytest.raises(ClaudeConnectionError):
        await _client(hass).async_get_status()
