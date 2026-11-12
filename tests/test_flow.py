from unittest.mock import AsyncMock, Mock

from custom_components.vandebron import _async_reload_entry
from custom_components.vandebron.config_flow import VandebronConfigFlow, _extract_code


def test_oauth_callback_requires_matching_state():
    good = "nl.vandebron.thuis.auth://oauth?code=abc&state=expected"
    assert _extract_code(good, "expected") == "abc"
    for value in (
        "abc",
        good.replace("expected", "wrong"),
        good.replace("oauth?", "other?"),
        good + "&code=second",
        good + "&error=denied",
    ):
        assert _extract_code(value, "expected") is None


async def test_invalid_callback_preserves_pkce(hass):
    flow = VandebronConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    first = await flow.async_step_oauth()
    verifier = flow._code_verifier
    url = first["description_placeholders"]["authorize_url"]
    second = await flow.async_step_oauth({"redirect_url": "bad"})
    assert second["errors"] == {"base": "invalid_callback"}
    assert second["description_placeholders"]["authorize_url"] == url
    assert flow._code_verifier == verifier


async def test_token_persistence_does_not_reload(hass, entry):
    hass.config_entries = Mock(async_reload=AsyncMock())
    entry.runtime_data = Mock(options={})
    await _async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()
    entry.options = {"include_vat": False}
    await _async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


async def test_reauth_updates_existing_entry(hass, monkeypatch):
    from homeassistant.config_entries import SOURCE_REAUTH

    from custom_components.vandebron.api import VandebronTokens

    flow = VandebronConfigFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_REAUTH}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_mismatch = Mock()
    flow._abort_if_unique_id_configured = Mock()
    flow._get_reauth_entry = Mock(return_value="existing")
    flow.async_update_reload_and_abort = Mock(
        return_value={"type": "abort", "reason": "reauth_successful"}
    )
    tokens = VandebronTokens("a", "r", customer_reference="same")
    result = await flow._async_create(tokens)
    assert result["reason"] == "reauth_successful"
    flow._abort_if_unique_id_mismatch.assert_called_once()
    flow._abort_if_unique_id_configured.assert_not_called()
    assert (
        flow.async_update_reload_and_abort.call_args.kwargs["data_updates"]["refresh_token"] == "r"
    )
