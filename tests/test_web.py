"""Tests for web answers: the ``web`` field of claude_ha.ask and the web agent."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import (
    ClaudeError,
    ClaudeRequestError,
    ClaudeWebDisabledError,
    ClaudeWebUnsupportedError,
    async_error_message,
)
from custom_components.claude_ha.confirm import DATA_PENDING_CHAT
from custom_components.claude_ha.const import (
    ATTR_INTENTS,
    ATTR_MODE,
    ATTR_NOTIFY,
    ATTR_PROMPT,
    ATTR_WEB,
    CONF_AUTO_EXECUTE,
    CONF_MEMORY,
    CONF_WEB,
    DOMAIN,
    ISSUE_WEB_DISABLED,
    MODE_WRITE,
    SERVICE_ASK,
)
from custom_components.claude_ha.issues import entry_issue_id
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, intent, issue_registry as ir

from .conftest import (
    ACCOUNT_LIMITS_PAYLOAD,
    REQUEST_FIELDS,
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    USAGE_PAYLOAD,
    setup_integration,
)

_PROPOSAL = {
    "summary": "Turn off the heater",
    "intents": [{"intent": "HassTurnOff", "targets": ["switch.heater"], "data": {}}],
}
# What a misbehaving add-on could answer a web question with.
_LOADED_ANSWER = {
    "text": "Sunrise is at 06:12.",
    "proposal": _PROPOSAL,
    "automation": {"alias": "x", "triggers": [], "actions": []},
    "memory_change": {"remember": ["I like it warm."]},
    "tools_used": ["mcp__ha__GetLiveContext"],
    "truncated": False,
}


@pytest.fixture
def web_status(aioclient_mock: AiohttpClientMocker) -> None:
    """Mock an add-on that lists the web and memory request fields."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status",
        json={**STATUS_PAYLOAD, "request_fields": [*REQUEST_FIELDS, "web", "memory"]},
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/account_limits", json=ACCOUNT_LIMITS_PAYLOAD
    )


@pytest.fixture
def web_entry(mock_config_entry: MockConfigEntry, web_status: None) -> MockConfigEntry:
    """Return an entry with the web agent on."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=mock_config_entry.title,
        unique_id=mock_config_entry.unique_id,
        version=mock_config_entry.version,
        minor_version=mock_config_entry.minor_version,
        data=dict(mock_config_entry.data),
        options={CONF_WEB: True, CONF_MEMORY: True, CONF_AUTO_EXECUTE: True},
    )


def _web_agent(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, f"{entry.entry_id}_web"
    )


def _posts(aioclient_mock: AiohttpClientMocker) -> list[dict[str, Any]]:
    return [c[2] for c in aioclient_mock.mock_calls if c[0] == "POST"]


async def _converse(
    hass: HomeAssistant, agent_id: str, text: str = "when is sunrise?"
) -> conversation.ConversationResult:
    return await conversation.async_converse(
        hass, text, None, context=Context(), agent_id=agent_id
    )


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, entry_issue_id(ISSUE_WEB_DISABLED, entry.entry_id)
    )


async def test_ask_web_sends_only_the_web_read(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A web ask carries web and nothing of the home; the answer is text only."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_LOADED_ANSWER)
    await setup_integration(hass, web_entry)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_ASK,
        {ATTR_PROMPT: "when is sunrise?", ATTR_WEB: True},
        blocking=True,
        return_response=True,
    )

    (body,) = _posts(aioclient_mock)
    assert body == {"prompt": "when is sunrise?", "mode": "read", "web": True}
    assert response == {
        "text": "Sunrise is at 06:12.",
        "proposal": None,
        "tools_used": [],
        "truncated": False,
    }


async def test_ask_without_web_never_sends_it(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_prompt: None,
) -> None:
    """The default ask is the house read: no web field."""
    await setup_integration(hass, web_entry)

    await hass.services.async_call(
        DOMAIN, SERVICE_ASK, {ATTR_PROMPT: "hi"}, blocking=True, return_response=True
    )

    assert "web" not in _posts(aioclient_mock)[-1]


@pytest.mark.parametrize(
    "extra",
    [
        {ATTR_MODE: MODE_WRITE, ATTR_INTENTS: _PROPOSAL["intents"]},
        {ATTR_NOTIFY: "mobile_app_phone"},
    ],
)
async def test_ask_web_is_a_plain_read(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    extra: dict[str, Any],
) -> None:
    """Web cannot be combined with write, intents or a confirmation notice."""
    await setup_integration(hass, web_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "x", ATTR_WEB: True, **extra},
            blocking=True,
            return_response=True,
        )

    assert err.value.translation_key == "web_requires_plain_read"
    assert _posts(aioclient_mock) == []


async def test_ask_web_refused_by_the_add_on(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A refused web ask raises the repair hint; a later answer clears it."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        status=403,
        json={"error": "off", "code": "web_disabled"},
    )
    await setup_integration(hass, web_entry)

    with pytest.raises(ClaudeWebDisabledError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ASK,
            {ATTR_PROMPT: "x", ATTR_WEB: True},
            blocking=True,
            return_response=True,
        )
    assert _issue(hass, web_entry) is not None

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_LOADED_ANSWER)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ASK,
        {ATTR_PROMPT: "x", ATTR_WEB: True},
        blocking=True,
        return_response=True,
    )
    assert _issue(hass, web_entry) is None


async def test_web_agent_exists_only_when_enabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    web_status: None,
) -> None:
    """Off by default; the toggle adds it and switching off removes it."""
    await setup_integration(hass, mock_config_entry)
    assert _web_agent(hass, mock_config_entry) is None

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert CONF_WEB in result["data_schema"].schema
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WEB: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    agent = _web_agent(hass, mock_config_entry)
    assert agent is not None
    assert hass.states.get(agent).attributes["friendly_name"] == "Claude Code (web)"

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WEB: False}
    )
    await hass.async_block_till_done()
    assert _web_agent(hass, mock_config_entry) is None


async def test_web_agent_answers_text_and_ignores_a_proposal(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A proposal injected into a web answer is never shown, held or executed."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_LOADED_ANSWER)
    await setup_integration(hass, web_entry)
    agent = _web_agent(hass, web_entry)
    assert agent is not None

    result = await _converse(hass, agent)

    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert result.response.speech["plain"]["speech"] == "Sunrise is at 06:12."
    # One request, a read with the web and no history, memory or write.
    (body,) = _posts(aioclient_mock)
    assert body["web"] is True
    assert body["mode"] == "read"
    for key in ("conversation_id", "memory", "intents", "confirmation", "image_entity"):
        assert key not in body
    # Nothing is held for a yes: the next turn is a fresh question, not an answer.
    assert hass.data.get(DATA_PENDING_CHAT, {}) == {}
    state = hass.states.get(agent)
    assert state is not None
    assert state.attributes["supported_features"] == 0


async def test_house_agent_never_sends_web(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_prompt: None,
) -> None:
    """With the web agent on, the house agent still asks only about the house."""
    await setup_integration(hass, web_entry)
    house = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, web_entry.entry_id
    )
    assert house is not None

    await _converse(hass, house, "how warm is the living room?")

    assert all("web" not in body for body in _posts(aioclient_mock))
    assert _posts(aioclient_mock)  # the control: a request was made


@pytest.mark.parametrize(
    ("status", "body", "message", "issue"),
    [
        (
            403,
            {"code": "web_disabled"},
            "Web answers are switched off in the add-on.",
            True,
        ),
        (
            501,
            {"code": "web_unsupported"},
            "This add-on cannot answer from the web.",
            False,
        ),
        (
            400,
            {"code": "mode_mismatch", "field": "web"},
            "The add-on rejected the web question.",
            False,
        ),
    ],
)
async def test_web_agent_says_why_it_was_refused(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    body: dict[str, str],
    message: str,
    issue: bool,
) -> None:
    """A refusal is a clear message in the chat; only a disabled option is a repair."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", status=status, json=body)
    await setup_integration(hass, web_entry)
    agent = _web_agent(hass, web_entry)
    assert agent is not None

    result = await _converse(hass, agent)

    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert message in result.response.speech["plain"]["speech"]
    assert (_issue(hass, web_entry) is not None) is issue


async def test_web_not_listed_is_refused_without_a_request(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    mock_status: None,
) -> None:
    """An add-on that does not list `web` is never sent the question to answer."""
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{TEST_BASE_URL}/api/status", json=STATUS_PAYLOAD)
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/account_limits", json=ACCOUNT_LIMITS_PAYLOAD
    )
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_LOADED_ANSWER)
    await setup_integration(hass, web_entry)
    agent = _web_agent(hass, web_entry)
    assert agent is not None

    result = await _converse(hass, agent)

    assert "cannot answer from the web" in result.response.speech["plain"]["speech"]
    assert _posts(aioclient_mock) == []


async def test_error_texts_exist(hass: HomeAssistant) -> None:
    """Every web error resolves to its own text, not the generic one."""
    generic = await async_error_message(hass, "en", ClaudeError())
    for error in (
        ClaudeWebDisabledError(),
        ClaudeWebUnsupportedError(),
        ClaudeRequestError(translation_key="web_rejected"),
    ):
        assert await async_error_message(hass, "en", error) != generic


async def test_web_refusal_without_a_caller_still_maps_a_400(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A bare 400 on the web path reads as a rejected web question."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", status=400)
    await setup_integration(hass, web_entry)
    client = web_entry.runtime_data.client

    with pytest.raises(ClaudeRequestError) as err:
        await client.async_prompt_web("x", caller="user-1", language="uk")

    assert err.value.translation_key == "web_rejected"
    assert aioclient_mock.mock_calls[-1][3]["X-Claude-Caller"] == "user-1"


async def test_client_keeps_only_the_text_of_a_web_answer(
    hass: HomeAssistant,
    web_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Whatever else the answer carried, the client hands back text alone."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={**_LOADED_ANSWER, "actions": [{"intent": "x", "ok": True}]},
    )
    await setup_integration(hass, web_entry)

    result = await web_entry.runtime_data.client.async_prompt_web("x")

    assert result.text == "Sunrise is at 06:12."
    assert result.proposal is None
    assert result.automation is None
    assert result.memory_change is None
    assert result.actions is None
    assert result.tools_used == []
