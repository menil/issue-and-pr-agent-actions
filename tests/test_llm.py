import json
import os
import urllib.error
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from llm import (
    ProviderConfig,
    build_provider_candidates,
    check_credentials,
    request_with_fallback,
)


def _env(**values: str) -> Any:
    """An environment with only these variables, whatever the machine has."""
    return patch.dict(os.environ, values, clear=True)


# --- check_credentials ---


def test_check_credentials_rejects_an_unknown_provider(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _env(), pytest.raises(SystemExit) as excinfo:
        check_credentials("gemini")
    assert excinfo.value.code == 1
    assert "PROVIDER must be" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("provider", "env"),
    [
        pytest.param("openrouter", {"ANTHROPIC_API_KEY": "k"}, id="openrouter"),
        pytest.param("anthropic", {"OPENROUTER_API_KEY": "k"}, id="anthropic"),
        pytest.param("auto", {}, id="auto-with-nothing"),
        pytest.param("auto", {"OPENROUTER_API_KEY": "  "}, id="auto-with-blank-key"),
    ],
)
def test_check_credentials_exits_without_what_the_provider_needs(
    provider: str, env: dict[str, str]
) -> None:
    with _env(**env), pytest.raises(SystemExit) as excinfo:
        check_credentials(provider)
    assert excinfo.value.code == 1


@pytest.mark.parametrize(
    ("provider", "env"),
    [
        pytest.param("openrouter", {"OPENROUTER_API_KEY": "k"}, id="openrouter"),
        pytest.param("anthropic", {"CLAUDE_CODE_OAUTH_TOKEN": "t"}, id="oauth-token"),
        pytest.param("anthropic", {"ANTHROPIC_API_KEY": "k"}, id="api-key"),
        pytest.param("auto", {"OPENROUTER_API_KEY": "k"}, id="auto-openrouter-only"),
        pytest.param("auto", {"CLAUDE_CODE_OAUTH_TOKEN": "t"}, id="auto-claude-only"),
    ],
)
def test_check_credentials_accepts_what_the_provider_needs(
    provider: str, env: dict[str, str]
) -> None:
    with _env(**env):
        check_credentials(provider)


# --- build_provider_candidates ---

_ALL_CREDENTIALS = {
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
    "ANTHROPIC_API_KEY": "key",
    "OPENROUTER_API_KEY": "or",
}


def test_candidates_try_the_subscription_then_the_key_then_openrouter() -> None:
    with _env(**_ALL_CREDENTIALS):
        candidates = build_provider_candidates("auto")
    assert [(c.name, c.api_key, c.use_oauth) for c in candidates] == [
        ("anthropic", "oauth", True),
        ("anthropic", "key", False),
        ("openrouter", "or", False),
    ]


@pytest.mark.parametrize(
    ("provider", "names"),
    [("anthropic", ["anthropic", "anthropic"]), ("openrouter", ["openrouter"])],
)
def test_candidates_are_limited_to_the_chosen_provider(
    provider: str, names: list[str]
) -> None:
    with _env(**_ALL_CREDENTIALS):
        assert [c.name for c in build_provider_candidates(provider)] == names


def test_candidates_use_the_defaults_and_ignore_a_bad_token_limit() -> None:
    with _env(OPENROUTER_API_KEY="or", OPENROUTER_MAX_TOKENS="lots"):
        (candidate,) = build_provider_candidates("auto")
    assert candidate.model == "openrouter/free"
    assert candidate.base_url == "https://openrouter.ai/api/v1/chat/completions"
    assert candidate.max_tokens == 8192


def test_candidates_read_the_configured_model() -> None:
    with _env(
        ANTHROPIC_API_KEY="k", ANTHROPIC_MODEL="claude-x", ANTHROPIC_MAX_TOKENS="99"
    ):
        (candidate,) = build_provider_candidates("anthropic")
    assert (candidate.model, candidate.max_tokens) == ("claude-x", 99)


# --- request_with_fallback ---


def _config(name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        api_key="k",
        model="m",
        base_url="u",
        max_tokens=1,
        auth_desc=f"{name} key",
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", MagicMock(), MagicMock())


def _sender(*outcomes: str | Exception) -> Callable[[ProviderConfig], str]:
    """A send function that gives these outcomes in order, one per call."""
    remaining = list(outcomes)

    def send(config: ProviderConfig) -> str:
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return send


@pytest.fixture(autouse=True)
def _no_sleep() -> Any:
    with patch("time.sleep"):
        yield


def test_the_first_provider_that_answers_wins() -> None:
    send = MagicMock(return_value='{"a": 1}')
    result, raw, error = request_with_fallback(
        [_config("anthropic"), _config("openrouter")], send, json.loads
    )
    assert (result, raw, error) == ({"a": 1}, '{"a": 1}', None)
    send.assert_called_once()


def test_a_providers_own_failure_moves_on_to_the_next(
    capsys: pytest.CaptureFixture[str],
) -> None:
    send = _sender(_http_error(401), '{"ok": true}')
    result, raw, _ = request_with_fallback(
        [_config("anthropic"), _config("openrouter")], send, json.loads
    )
    assert result == {"ok": True}
    assert "Falling back to 'openrouter'" in capsys.readouterr().err


def test_a_request_error_is_retried_and_does_not_move_on() -> None:
    """Another provider would get the same bad request, so it is not asked."""
    send = MagicMock(side_effect=ValueError("HTTP Error 400: bad request"))
    result, raw, error = request_with_fallback(
        [_config("anthropic"), _config("openrouter")], send, json.loads
    )
    assert result is None
    assert send.call_count == 3
    assert {call.args[0].name for call in send.call_args_list} == {"anthropic"}
    assert isinstance(error, ValueError)


def test_a_reply_that_does_not_parse_is_retried_with_the_same_provider() -> None:
    send = _sender("not json", '{"ok": 1}')
    result, raw, _ = request_with_fallback([_config("openrouter")], send, json.loads)
    assert (result, raw) == ({"ok": 1}, '{"ok": 1}')


def test_when_nothing_parses_the_last_reply_is_returned_for_salvage() -> None:
    send = MagicMock(return_value="not json")
    result, raw, error = request_with_fallback(
        [_config("openrouter")], send, json.loads
    )
    assert (result, raw) == (None, "not json")
    assert isinstance(error, json.JSONDecodeError)


def test_when_every_attempt_errors_there_is_no_reply_to_return() -> None:
    send = MagicMock(side_effect=ValueError("boom"))
    result, raw, error = request_with_fallback(
        [_config("openrouter")], send, json.loads
    )
    assert (result, raw) == (None, "")
    assert str(error) == "boom"


def test_with_no_providers_there_is_nothing_to_return() -> None:
    assert request_with_fallback([], MagicMock(), json.loads) == (None, "", None)


def test_the_log_names_what_is_being_requested(
    capsys: pytest.CaptureFixture[str],
) -> None:
    send = _sender(ValueError("boom"), "{}")
    request_with_fallback([_config("openrouter")], send, json.loads, what="triage")
    err = capsys.readouterr()
    assert "Requesting triage from provider 'openrouter'" in err.out
    assert "Triage attempt 1 failed" in err.err


def test_a_message_that_quotes_text_cannot_start_a_log_line_with_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    send = MagicMock(side_effect=ValueError("quoted\n::error::pwned"))
    request_with_fallback([_config("openrouter")], send, json.loads)
    err = capsys.readouterr().err
    assert not [line for line in err.splitlines() if line.lstrip().startswith("::")]
    assert "pwned" in err
