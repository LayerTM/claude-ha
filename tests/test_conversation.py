"""Tests for the Claude conversation agent."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.claude_ha.api import ClaudeError, Proposal
from custom_components.claude_ha.const import DOMAIN, HEADER_CALLER
from custom_components.claude_ha.conversation import (
    _delete_query,
    _render_automation_draft,
    _render_proposal,
    _spoken_confirm,
)
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er, intent

from .conftest import (
    STATUS_PAYLOAD,
    TEST_BASE_URL,
    USAGE_PAYLOAD,
    setup_integration,
)

# A voice satellite entity id — HA sets this on ConversationInput only for turns
# that arrive through an assist_satellite (audio in, TTS out).
VOICE_SATELLITE = "assist_satellite.kitchen"


def _agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of this integration's conversation agent."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id
    )
    assert entity_id is not None
    return entity_id


async def test_conversation_reply(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_prompt: None,
) -> None:
    """A chat turn forwards to the add-on and returns Claude's answer."""
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "How warm is the living room?",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
    )

    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert "living room is 21" in result.response.speech["plain"]["speech"]


async def test_conversation_forwards_language(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The turn's language is forwarded so the add-on can localize its messages."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    await conversation.async_converse(
        hass,
        "what's the weather?",
        None,
        context=Context(),
        language="uk",
        agent_id=_agent_id(hass, mock_config_entry),
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2]["language"] == "uk"


async def test_conversation_surfaces_proposal(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A proposal without a low-risk hint is surfaced and confirmed, not run."""
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={
            "text": "You asked me to turn off the heater.",
            "proposal": {
                "summary": "Turn off the heater",
                "intents": [
                    {"intent": "HassTurnOff", "targets": ["switch.heater"], "data": {}}
                ],
            },
            "tools_used": [],
            "truncated": False,
        },
    )
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "turn off the heater",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
    )
    speech = result.response.speech["plain"]["speech"]
    assert "Turn off the heater" in speech
    assert "switch.heater" in speech
    assert "Confirm? (yes/no)" in speech


async def test_conversation_supported_languages(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
) -> None:
    """The agent advertises support for all languages."""
    from custom_components.claude_ha.conversation import ClaudeConversationEntity
    from homeassistant.const import MATCH_ALL

    await setup_integration(hass, mock_config_entry)
    entity = ClaudeConversationEntity(mock_config_entry.runtime_data.status)
    assert entity.supported_languages == MATCH_ALL


async def test_conversation_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An add-on error is returned as an error response, not an exception."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", status=503)
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "hello",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
    )
    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_conversation_propagates_id_and_caller(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    mock_prompt: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The entity forwards the caller and reuses the conversation id."""
    await setup_integration(hass, mock_config_entry)
    agent = _agent_id(hass, mock_config_entry)

    first = await conversation.async_converse(
        hass, "hi", None, context=Context(user_id="user-42"), agent_id=agent
    )
    body = aioclient_mock.mock_calls[-1][2]
    headers = aioclient_mock.mock_calls[-1][3]
    assert body["conversation_id"] == first.conversation_id
    assert headers[HEADER_CALLER] == "user-42"

    await conversation.async_converse(
        hass,
        "and again",
        first.conversation_id,
        context=Context(user_id="user-42"),
        agent_id=agent,
    )
    assert aioclient_mock.mock_calls[-1][2]["conversation_id"] == first.conversation_id


def test_render_proposal_no_proposal() -> None:
    """With no proposal, the reply is Claude's text verbatim."""
    assert _render_proposal("just an answer", None) == "just an answer"


@pytest.mark.parametrize(
    ("text", "proposal", "expect_present", "expect_absent"),
    [
        (
            "answer",
            Proposal(summary="", intents=[]),
            ["answer"],
            ["Proposed:", "Affects:"],
        ),
        (
            "answer",
            Proposal(summary="Do X", intents=[{"intent": "HassX"}]),
            ["answer", "Proposed: Do X"],
            ["Affects:"],
        ),
        (
            "",
            Proposal(summary="Do X", intents=[{"targets": ["light.x"]}]),
            ["Proposed: Do X", "Affects: light.x"],
            [],
        ),
    ],
)
def test_render_proposal_variants(
    text: str,
    proposal: Proposal,
    expect_present: list[str],
    expect_absent: list[str],
) -> None:
    """Proposal rendering omits empty parts (no execute/disclaimer text)."""
    reply = _render_proposal(text, proposal)
    for fragment in expect_present:
        assert fragment in reply
    for fragment in expect_absent:
        assert fragment not in reply


def _mock_status(aioclient_mock: AiohttpClientMocker, version: str) -> None:
    """Register /api/status (with a chosen add-on version) and /api/usage."""
    aioclient_mock.get(
        f"{TEST_BASE_URL}/api/status", json={**STATUS_PAYLOAD, "version": version}
    )
    aioclient_mock.get(f"{TEST_BASE_URL}/api/usage", json=USAGE_PAYLOAD)


@pytest.mark.parametrize(
    ("satellite_id", "expected"),
    [(VOICE_SATELLITE, "voice"), (None, "text")],
)
async def test_conversation_forwards_surface(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    satellite_id: str | None,
    expected: str,
) -> None:
    """A new-enough add-on gets surface=voice for satellite turns, text otherwise."""
    _mock_status(aioclient_mock, "1.28.0")
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    await conversation.async_converse(
        hass,
        "what's the weather?",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        satellite_id=satellite_id,
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert posts[-1][2]["surface"] == expected


async def test_conversation_omits_surface_for_old_addon(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An add-on below 1.28.0 rejects unknown keys, so surface is never sent."""
    _mock_status(aioclient_mock, "1.27.9")
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={"text": "ok", "proposal": None, "tools_used": [], "truncated": False},
    )
    await setup_integration(hass, mock_config_entry)

    await conversation.async_converse(
        hass,
        "what's the weather?",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        satellite_id=VOICE_SATELLITE,
    )

    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert "surface" not in posts[-1][2]


async def test_voice_confirmation_is_spoken_friendly(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A voice proposal is confirmed with a short spoken prompt, not markdown."""
    _mock_status(aioclient_mock, "1.28.0")
    aioclient_mock.post(
        f"{TEST_BASE_URL}/api/prompt",
        json={
            "text": "You asked me to turn off the heater.",
            "proposal": {
                "summary": "Turn off the heater",
                "intents": [
                    {"intent": "HassTurnOff", "targets": ["switch.heater"], "data": {}}
                ],
            },
            "tools_used": [],
            "truncated": False,
        },
    )
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "turn off the heater",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        satellite_id=VOICE_SATELLITE,
    )
    speech = result.response.speech["plain"]["speech"]
    assert "Say yes to confirm" in speech
    # None of the aloud-noisy affordances leak into a spoken turn.
    assert "Confirm? (yes/no)" not in speech
    assert "switch.heater" not in speech
    assert "Affects:" not in speech


def test_spoken_confirm_keeps_unstreamed_answer() -> None:
    """When the read hasn't streamed, the answer is kept before the confirm line."""
    assert _spoken_confirm("It is 21 degrees.", "Turn on the fan") == (
        "It is 21 degrees. Turn on the fan. Say yes to confirm."
    )
    assert (
        _spoken_confirm("", "Turn on the fan") == "Turn on the fan. Say yes to confirm."
    )


_DRAFT_BODY = {
    "text": "I drafted an automation for you.",
    "proposal": None,
    "automation": {
        "alias": "Morning greeting",
        "triggers": [{"trigger": "time", "at": "08:00:00"}],
        "actions": [{"action": "notify.notify", "data": {"message": "Good morning!"}}],
        "mode": "single",
    },
    "tools_used": [],
    "truncated": False,
}


async def _draft_then(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    second_text: str,
) -> conversation.ConversationResult:
    """Draft an automation (turn 1), then reply ``second_text`` (turn 2, same convo)."""
    agent = _agent_id(hass, entry)
    first = await conversation.async_converse(
        hass,
        "create an automation that greets me at 8am",
        None,
        context=Context(),
        agent_id=agent,
    )
    return await conversation.async_converse(
        hass, second_text, first.conversation_id, context=Context(), agent_id=agent
    )


async def test_automation_draft_shows_yaml_and_asks_confirm(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A drafted automation shows the YAML and asks to confirm — no write on turn 1."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_DRAFT_BODY)
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "create an automation that greets me at 8am",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
    )

    speech = result.response.speech["plain"]["speech"]
    assert "Morning greeting" in speech
    assert "```yaml" in speech
    assert "notify.notify" in speech
    assert "Create it? (yes/no)" in speech
    # Drafting never writes: no write POST, and nothing is committed on this turn.
    posts = [c for c in aioclient_mock.mock_calls if c[0] == "POST"]
    assert all(c[2].get("mode") != "write" for c in posts)


async def test_automation_draft_voice_asks_spoken_confirm(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """On voice, the draft asks a short spoken confirmation — no YAML read aloud."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_DRAFT_BODY)
    await setup_integration(hass, mock_config_entry)

    result = await conversation.async_converse(
        hass,
        "create an automation that greets me at 8am",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        satellite_id=VOICE_SATELLITE,
    )

    speech = result.response.speech["plain"]["speech"]
    assert "```yaml" not in speech
    assert "Create automation Morning greeting" in speech
    assert "Say yes to confirm" in speech


async def test_automation_confirm_routes_to_commit(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirming a drafted automation runs the in-process commit and reports it."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_DRAFT_BODY)
    committed: list[dict] = []

    async def _fake_commit(_hass: HomeAssistant, automation: dict) -> str:
        committed.append(automation)
        return str(automation["alias"])

    monkeypatch.setattr(
        "custom_components.claude_ha.conversation.async_commit_automation", _fake_commit
    )
    await setup_integration(hass, mock_config_entry)

    result = await _draft_then(hass, mock_config_entry, "yes")

    assert committed and committed[0]["alias"] == "Morning greeting"
    assert (
        "Created automation: Morning greeting"
        in result.response.speech["plain"]["speech"]
    )


async def test_automation_commit_failure_is_a_clean_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected/failed commit returns a clean error response, never raises."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_DRAFT_BODY)

    async def _fake_commit(_hass: HomeAssistant, _automation: dict) -> str:
        raise ClaudeError("that automation isn't allowed")

    monkeypatch.setattr(
        "custom_components.claude_ha.conversation.async_commit_automation", _fake_commit
    )
    await setup_integration(hass, mock_config_entry)

    result = await _draft_then(hass, mock_config_entry, "yes")
    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_automation_decline_cancels_without_committing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining a drafted automation cancels it and never calls the commit."""
    aioclient_mock.post(f"{TEST_BASE_URL}/api/prompt", json=_DRAFT_BODY)

    async def _fail_commit(_hass: HomeAssistant, _automation: dict) -> str:
        raise AssertionError("commit must not run on decline")

    monkeypatch.setattr(
        "custom_components.claude_ha.conversation.async_commit_automation", _fail_commit
    )
    await setup_integration(hass, mock_config_entry)

    result = await _draft_then(hass, mock_config_entry, "no")
    assert "Cancelled" in result.response.speech["plain"]["speech"]


def test_render_automation_draft_includes_alias_and_yaml() -> None:
    """The render carries the alias lead-in and a faithful YAML block."""
    out = _render_automation_draft(
        "",
        {
            "alias": "X",
            "triggers": [{"trigger": "time", "at": "08:00:00"}],
            "actions": [{"action": "notify.notify"}],
        },
    )
    assert "draft automation — **X**" in out
    assert "```yaml" in out
    assert "trigger: time" in out  # yaml body rendered, keys not reordered


def test_render_automation_draft_keeps_prose_and_defaults_alias() -> None:
    """Prose is kept above the block; a missing alias falls back to 'automation'."""
    out = _render_automation_draft("Here you go.", {"triggers": [], "actions": []})
    assert out.startswith("Here you go.")
    assert "**automation**" in out


def test_delete_query_detects_delete_requests() -> None:
    """The delete intercept fires only on a delete verb + 'automation', not a create."""
    q = _delete_query("delete my morning lights automation")
    assert q is not None and "morning" in q and "lights" in q and "delete" not in q
    assert "coffee" in (_delete_query("remove the automation named coffee") or "")
    # Ukrainian delete phrasing
    uk = _delete_query("видали автоматизацію ранкове світло")
    assert uk is not None and "ранкове" in uk
    # not a delete
    assert _delete_query("what's the weather?") is None  # no delete verb
    assert _delete_query("delete the kitchen light") is None  # no "automation"
    # a create verb wins even with a delete verb present
    assert _delete_query("create an automation to delete old logs") is None


async def _delete_turn(
    hass: HomeAssistant, entry: MockConfigEntry, text: str, conv_id: str | None = None
) -> conversation.ConversationResult:
    return await conversation.async_converse(
        hass, text, conv_id, context=Context(), agent_id=_agent_id(hass, entry)
    )


async def test_delete_flow_confirms_then_deletes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delete request confirms the matched automation, then deletes it on 'yes'."""
    await setup_integration(hass, mock_config_entry)
    hass.states.async_set(
        "automation.m", "on", {"id": "id-m", "friendly_name": "Morning Lights"}
    )
    deleted: list[str] = []

    async def _fake_delete(_hass: HomeAssistant, config_id: str) -> None:
        deleted.append(config_id)

    monkeypatch.setattr(
        "custom_components.claude_ha.conversation.async_delete_automation", _fake_delete
    )

    first = await _delete_turn(
        hass, mock_config_entry, "delete my morning lights automation"
    )
    assert (
        "Delete the automation 'Morning Lights'?"
        in first.response.speech["plain"]["speech"]
    )
    second = await _delete_turn(hass, mock_config_entry, "yes", first.conversation_id)
    assert deleted == ["id-m"]
    assert (
        "Deleted automation: Morning Lights"
        in second.response.speech["plain"]["speech"]
    )


async def test_delete_flow_ambiguous_asks_which(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_status: None
) -> None:
    """More than one match asks which one, and stores no pending delete."""
    await setup_integration(hass, mock_config_entry)
    hass.states.async_set(
        "automation.k", "on", {"id": "k", "friendly_name": "Kitchen Lights"}
    )
    hass.states.async_set(
        "automation.b", "on", {"id": "b", "friendly_name": "Bedroom Lights"}
    )

    result = await _delete_turn(hass, mock_config_entry, "delete the lights automation")
    speech = result.response.speech["plain"]["speech"]
    assert "which" in speech.lower()
    assert "Kitchen Lights" in speech and "Bedroom Lights" in speech


async def test_delete_flow_no_match(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_status: None
) -> None:
    """A delete request that matches nothing says so and does not act."""
    await setup_integration(hass, mock_config_entry)
    result = await _delete_turn(
        hass, mock_config_entry, "delete my nonexistent automation"
    )
    assert "couldn't find" in result.response.speech["plain"]["speech"]


async def test_delete_flow_decline_cancels(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declining a delete cancels it and never calls the delete."""
    await setup_integration(hass, mock_config_entry)
    hass.states.async_set(
        "automation.m", "on", {"id": "id-m", "friendly_name": "Morning Lights"}
    )

    async def _fail(_hass: HomeAssistant, _config_id: str) -> None:
        raise AssertionError("delete must not run on decline")

    monkeypatch.setattr(
        "custom_components.claude_ha.conversation.async_delete_automation", _fail
    )

    first = await _delete_turn(hass, mock_config_entry, "delete my morning automation")
    assert "Delete the automation" in first.response.speech["plain"]["speech"]
    second = await _delete_turn(hass, mock_config_entry, "no", first.conversation_id)
    assert "Cancelled" in second.response.speech["plain"]["speech"]


async def test_delete_flow_voice_uses_spoken_confirm(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_status: None
) -> None:
    """On voice, the delete confirmation is a short spoken prompt (no markdown)."""
    await setup_integration(hass, mock_config_entry)
    hass.states.async_set(
        "automation.m", "on", {"id": "id-m", "friendly_name": "Morning Lights"}
    )
    result = await conversation.async_converse(
        hass,
        "delete my morning lights automation",
        None,
        context=Context(),
        agent_id=_agent_id(hass, mock_config_entry),
        satellite_id=VOICE_SATELLITE,
    )
    speech = result.response.speech["plain"]["speech"]
    assert "Delete automation Morning Lights" in speech
    assert "Say yes to confirm" in speech


async def test_delete_commit_failure_is_a_clean_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_status: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure during the confirmed delete is a clean error response, not a raise."""
    await setup_integration(hass, mock_config_entry)
    hass.states.async_set(
        "automation.m", "on", {"id": "id-m", "friendly_name": "Morning Lights"}
    )

    async def _boom(_hass: HomeAssistant, _config_id: str) -> None:
        raise ClaudeError("couldn't delete")

    monkeypatch.setattr(
        "custom_components.claude_ha.conversation.async_delete_automation", _boom
    )

    first = await _delete_turn(
        hass, mock_config_entry, "delete my morning lights automation"
    )
    result = await _delete_turn(hass, mock_config_entry, "yes", first.conversation_id)
    assert result.response.response_type is intent.IntentResponseType.ERROR
