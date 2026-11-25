"""Talk to the language model providers: Claude, then OpenRouter as the fallback.

Everything here is independent of what is being asked, so every action in this
repository shares it: provider selection from the environment, one request to a
provider with retries and backoff, and the loop that moves on to the next provider
when one fails for a reason that is its own. Each action supplies its own prompt.
"""

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, TypeVar

from sanitize import log_text


def _calculate_backoff(
    attempt: int, base_delay: float, retry_after: str | float | int | None = None
) -> float:
    """Calculate jittered exponential backoff delay, respecting optional retry-after metadata.

    Jittered exponential backoff prevents synchronized retry thundering herds.
    """
    delay = base_delay * (2**attempt) + random.uniform(0.1, 1.0)
    if retry_after is not None:
        try:
            # Respect the provider's suggested wait time if it is longer than our backoff
            delay = max(delay, float(retry_after))
        except (ValueError, TypeError):
            pass
    # Cap the maximum delay to prevent excessively long blocks in automated workflows
    return float(min(delay, 60.0))


def _send_request(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    provider: str = "openrouter",
) -> str:
    """Send an HTTP request to the configured LLM provider and handle errors/timeouts.

    Args:
        url: The target completions/messages API endpoint URL.
        payload: The request body containing model instructions and settings.
        headers: Provider-specific headers (Authorization for OpenRouter, x-api-key for Anthropic).
        provider: Which response schema to parse — "openrouter" (OpenAI-compatible chat
            completions) or "anthropic" (Messages API).

    Returns:
        The text content returned by the model (chat completion choice for OpenRouter,
        concatenated text blocks for Anthropic).

    Raises:
        ValueError: If the response is empty, malformed, a refusal, or on HTTPError.
        urllib.error.URLError: If connection/DNS resolution fails and retries are exhausted.
        TimeoutError: If request times out and retries are exhausted.
    """
    max_retries = 5
    base_delay = 1.0

    # Serialize and encode payload once outside the retry loop for performance
    data = json.dumps(payload).encode("utf-8")

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                res_content = res.read(10 * 1024 * 1024).decode(
                    "utf-8", errors="replace"
                )
                res_data = json.loads(res_content)

                if provider == "anthropic":
                    # The Anthropic API signals failures with HTTP status codes (handled in the
                    # except blocks below), but defensively handle an error object returned with
                    # a 200 status as well.
                    if isinstance(res_data, dict) and res_data.get("type") == "error":
                        err = res_data.get("error")
                        err_msg = (
                            str(err.get("message") or "")
                            if isinstance(err, dict)
                            else str(err)
                        )
                        raise ValueError(f"Anthropic Error: {err_msg}")

                    # Safety classifiers may decline a request with HTTP 200, stop_reason
                    # "refusal", and no usable content. Surface it so the attempt retries/fails.
                    if res_data.get("stop_reason") == "refusal":
                        raise ValueError(
                            "Anthropic refused to process the request (stop_reason: refusal)."
                        )

                    content_blocks = res_data.get("content", [])
                    if not content_blocks:
                        truncated_res = (
                            res_content[:1000] + "..."
                            if len(res_content) > 1000
                            else res_content
                        )
                        raise ValueError(
                            f"Anthropic returned empty content. Full response: {truncated_res}"
                        )

                    # Concatenate every text block; non-text blocks (e.g. thinking) are ignored.
                    text_parts = [
                        block["text"]
                        for block in content_blocks
                        if isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ]
                    if not text_parts:
                        raise ValueError(
                            f"Anthropic returned no text content. Full response: {res_content}"
                        )
                    return "".join(text_parts)

                # Default provider: OpenRouter (OpenAI-compatible chat completions).
                if isinstance(res_data, dict) and "error" in res_data:
                    err = res_data["error"]
                    err_msg = (
                        str(err.get("message") or "")
                        if isinstance(err, dict)
                        else str(err)
                    )
                    err_code = err.get("code") if isinstance(err, dict) else None

                    # Some API providers return rate-limit info in the JSON response body
                    # with HTTP 200 rather than as an HTTP 429 status code.
                    if (
                        err_code == 429 or "rate limit" in err_msg.lower()
                    ) and attempt < max_retries:
                        retry_after = None
                        if isinstance(err, dict):
                            metadata = err.get("metadata")
                            if isinstance(metadata, dict):
                                retry_after = metadata.get("retry_after_seconds")

                        delay = _calculate_backoff(attempt, base_delay, retry_after)
                        print(
                            f"Rate limited by API error. Retrying in {delay:.2f} seconds...",
                            file=sys.stderr,
                        )
                        time.sleep(delay)
                        continue

                    raise ValueError(f"OpenRouter Error: {err_msg}")

                choices = res_data.get("choices", [])
                if not choices:
                    truncated_res = (
                        res_content[:1000] + "..."
                        if len(res_content) > 1000
                        else res_content
                    )
                    raise ValueError(
                        f"OpenRouter returned empty choices. Full response: {truncated_res}"
                    )
                message = choices[0].get("message", {})
                content = message.get("content")
                if content is None:
                    refusal = message.get("refusal")
                    if refusal:
                        raise ValueError(f"OpenRouter Refusal: {refusal}")
                    raise ValueError(
                        f"OpenRouter returned message with null content. Full response: {res_content}"
                    )
                if not isinstance(content, str):
                    raise ValueError("OpenRouter returned non-string message content.")
                return content
        except urllib.error.HTTPError as e:
            # Read the error body eagerly once to avoid stream exhaustion issues on retry/raise paths
            try:
                err_body = e.read(1024 * 1024).decode("utf-8", errors="replace")
            except Exception:
                err_body = ""

            # Only retry transient errors (429 rate limit or 5xx server errors).
            # Permanent errors (e.g. 400 Bad Request, 401 Unauthorized, 403 Forbidden) fail immediately.
            is_retryable = e.code == 429 or (500 <= e.code < 600)
            if is_retryable and attempt < max_retries:
                retry_after = e.headers.get("Retry-After")

                # Attempt to parse body metadata for specific retry details in case standard header is not populated
                if err_body:
                    try:
                        err_data = json.loads(err_body)
                        if isinstance(err_data, dict):
                            err_info = err_data.get("error")
                            if isinstance(err_info, dict):
                                metadata = err_info.get("metadata")
                                if isinstance(metadata, dict):
                                    body_retry_after = metadata.get(
                                        "retry_after_seconds"
                                    )
                                    if body_retry_after is not None:
                                        retry_after = body_retry_after
                    except Exception:
                        pass

                delay = _calculate_backoff(attempt, base_delay, retry_after)
                print(
                    f"API request failed with status {e.code}. Retrying in {delay:.2f} seconds...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            else:
                if not err_body:
                    err_body = "<could not read HTTP error body>"
                provider_name = "Anthropic" if provider == "anthropic" else "OpenRouter"
                raise ValueError(
                    f"{provider_name} HTTP Error {e.code}: {err_body}"
                ) from e
        except (urllib.error.URLError, TimeoutError) as e:
            # Network drops and timeouts are transient issues; retry them
            if attempt < max_retries:
                delay = _calculate_backoff(attempt, base_delay)
                print(
                    f"API request network/timeout error: {log_text(e)}. Retrying in {delay:.2f} seconds...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            else:
                # Propagate connection errors once max retries are exhausted
                raise

    raise ValueError("API request failed: retries exhausted.")


class ProviderConfig:
    """Configuration for an LLM provider candidate attempt."""

    def __init__(
        self,
        name: str,
        api_key: str,
        model: str,
        base_url: str,
        max_tokens: int,
        use_oauth: bool = False,
        auth_desc: str = "",
    ) -> None:
        self.name = name
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.use_oauth = use_oauth
        self.auth_desc = auth_desc


def is_token_or_server_error(error: Exception) -> bool:
    """Determine whether an error is an authentication/token error or a 5xx/server error.

    Returns True for:
      - HTTP 401 (Unauthorized: invalid, expired, or missing credentials)
      - HTTP 402 (Payment Required: depleted credits/balance on OpenRouter)
      - HTTP 403 (Forbidden: inactive subscription, permission denied)
      - HTTP 429 (Too Many Requests: rate limit or quota exceeded)
      - HTTP 5xx (500 <= status < 600, including 500, 502, 503, 504, 529 Overloaded)
      - Depleted balance / credit exhaustion (including Anthropic's HTTP 400 credit balance error)
      - Connection drops, DNS resolution failures, and request timeouts (URLError, TimeoutError)
      - API error messages indicating authentication, permission, rate limit, quota, or server overload

    Returns False for client/request errors (e.g., standard HTTP 400 Bad Request, 404 Not Found, 413, 422,
    invalid parameters, model refusals, or JSON formatting errors).
    """
    err_str = str(error)
    lower_err = err_str.lower()

    # Credit exhaustion, billing, balance depletion, and account quota errors must trigger
    # fallback even when returned under HTTP 400 with invalid_request_error (as done by Anthropic):
    # "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing..."
    if any(
        k in lower_err
        for k in (
            "credit balance",
            "credit_balance",
            "insufficient credits",
            "insufficient_quota",
            "quota",
            "billing",
            "purchase credits",
            "plans & billing",
            "payment required",
            "account suspended",
            "account deactivated",
        )
    ):
        return True

    # 1. Traverse full exception chain (__cause__ and __context__) for HTTPError, URLError, TimeoutError
    curr: BaseException | None = error
    visited = set()
    while curr is not None and id(curr) not in visited:
        visited.add(id(curr))
        if isinstance(curr, urllib.error.HTTPError):
            if curr.code in (401, 402, 403, 429) or (500 <= curr.code < 600):
                return True
            return False
        if isinstance(curr, (urllib.error.URLError, TimeoutError)):
            return True
        curr = curr.__cause__ or curr.__context__

    # 3. Check for HTTP status code in error message (e.g. "HTTP Error 401", "HTTP status 500")
    http_code_match = re.search(
        r"HTTP (?:Error|status)\s*(\d{3})", err_str, re.IGNORECASE
    )
    if http_code_match:
        code = int(http_code_match.group(1))
        if code in (401, 402, 403, 429) or (500 <= code < 600):
            return True
        return False

    # 4. Check for known token and server error types or phrases
    # Explicit non-token/client error patterns should never trigger fallback
    if any(
        k in lower_err
        for k in ("invalid_request_error", "bad request", "stop_reason: refusal")
    ):
        return False

    token_or_server_patterns = [
        r"authentication_error",
        r"permission_error",
        r"rate_limit_error",
        r"overloaded_error",
        r"api_error",
        r"\b(?:invalid|expired|revoked|missing)\s+(?:api[_-]?key|token|credentials?)\b",
        r"\btoken\s+(?:has\s+)?expired\b",
        r"\bunauthorized\b",
        r"\brate\s+limit(?:ed)?\b",
        r"\bquota\s+(?:exceeded|limit)\b",
        r"\bcredit\s+balance\b",
        r"\boverloaded\b",
        r"\binternal\s+server\s+error\b",
        r"\bbad\s+gateway\b",
        r"\bservice\s+unavailable\b",
        r"\bgateway\s+timeout\b",
    ]
    if any(re.search(pat, lower_err) for pat in token_or_server_patterns):
        return True

    return False


def openrouter_chat(
    api_key: str,
    system_instruction: str,
    user_message: str,
    model: str = "openrouter/free",
    base_url: str = "https://openrouter.ai/api/v1/chat/completions",
    max_tokens: int = 4096,
    repo: str = "issue-and-pr-agent-actions",
    what: str = "review",
) -> str:
    """Send a system prompt and a user message to OpenRouter and return the reply text.

    Asks for JSON mode first, and retries once without it if the endpoint rejects it.
    `what` only words the log ("Requesting review from ...").
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": f"https://github.com/{repo}",
        "X-Title": "PR Review Bot",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }

    # Instruct OpenRouter to exclude reasoning tokens from the text response.
    # This prevents reasoning text from polluting the returned completion string and
    # consuming unnecessary token limits when only the final JSON is required.
    if base_url and "openrouter.ai" in base_url.lower():
        payload["reasoning"] = {"exclude": True}

    print(f"Requesting {what} from OpenRouter using model: {model} (JSON mode)...")
    try:
        return _send_request(base_url, payload, headers)
    except Exception as e:
        err_msg = str(e)
        is_json_error = (
            "HTTP Error 400" in err_msg
            or "HTTP Error 422" in err_msg
            or isinstance(e, ValueError)
        )
        if is_json_error:
            print(
                f"Warning: JSON-mode request failed: {log_text(e)}. Retrying without strict JSON format constraint...",
                file=sys.stderr,
            )
            payload_fallback = payload.copy()
            payload_fallback.pop("response_format", None)
            # Remove the OpenRouter-specific reasoning parameter to prevent API validation errors (HTTP 400/422)
            # when attempting a non-strict fallback call or when using non-OpenRouter endpoints.
            payload_fallback.pop("reasoning", None)
            try:
                return _send_request(base_url, payload_fallback, headers)
            except Exception as fallback_err:
                print(
                    f"Error: Fallback request also failed: {log_text(fallback_err)}",
                    file=sys.stderr,
                )
                raise fallback_err
        else:
            raise


def anthropic_chat(
    api_key: str,
    system_instruction: str,
    user_message: str,
    model: str = "claude-haiku-4-5",
    base_url: str = "https://api.anthropic.com/v1/messages",
    max_tokens: int = 8192,
    use_oauth: bool = False,
    what: str = "review",
) -> str:
    """Send a system prompt and a user message to the Anthropic Messages API.

    Args:
        api_key: An Anthropic API key, or (when use_oauth is True) a Claude Pro/Max
            subscription OAuth token generated via `claude setup-token`.
        use_oauth: When True, authenticate the way the Claude Code CLI does for
            subscription auth: a Bearer token plus the "oauth-2025-04-20" beta header,
            instead of the standard x-api-key header used for billed API keys.
        what: Only words the log ("Requesting review from ...").
    """
    if use_oauth:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
        }
    else:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    if use_oauth:
        # Anthropic scopes Claude Pro/Max OAuth tokens to the Claude Code product: the API
        # rejects OAuth-authenticated requests unless the system prompt opens with Claude
        # Code's own identity line, so it must be prepended ahead of our own instructions.
        system_instruction = (
            "You are Claude Code, Anthropic's official CLI for Claude.\n\n"
            + system_instruction
        )

    # The Anthropic Messages API takes the system prompt as a top-level field (not a message)
    # and does not accept a JSON-mode response_format flag, so the JSON contract is enforced
    # entirely through the prompt and validated by the caller. `temperature` is intentionally
    # omitted: it is rejected with HTTP 400 on the Opus 4.x / Sonnet 5 tiers, so leaving it unset
    # keeps the action working across any configured Claude model.
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_instruction,
        "messages": [{"role": "user", "content": user_message}],
    }

    auth_desc = "Claude Pro/Max OAuth token" if use_oauth else "API key"
    print(
        f"Requesting {what} from Anthropic using model: {model} (auth: {auth_desc})..."
    )
    return _send_request(base_url, payload, headers, provider="anthropic")


def check_credentials(provider: str) -> None:
    """Exit with an error unless `provider` is valid and has the credentials it needs.

    `auto` needs at least one of the three credentials; `anthropic` needs an OAuth
    token or an API key; `openrouter` needs its API key.
    """
    if provider not in ("openrouter", "anthropic", "auto"):
        print(
            f"Error: PROVIDER must be 'auto', 'anthropic', or 'openrouter', got '{provider}'.",
            file=sys.stderr,
        )
        sys.exit(1)

    claude_code_oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()

    if provider == "openrouter":
        if not openrouter_api_key:
            print(
                "Error: OPENROUTER_API_KEY environment variable is required.",
                file=sys.stderr,
            )
            sys.exit(1)
    elif provider == "anthropic":
        if not claude_code_oauth_token and not anthropic_api_key:
            print(
                "Error: provider is 'anthropic' but neither ANTHROPIC_API_KEY nor "
                "CLAUDE_CODE_OAUTH_TOKEN environment variable is set.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:  # auto
        if (
            not claude_code_oauth_token
            and not anthropic_api_key
            and not openrouter_api_key
        ):
            print(
                "Error: No LLM credentials provided. Please set CLAUDE_CODE_OAUTH_TOKEN, "
                "ANTHROPIC_API_KEY, or OPENROUTER_API_KEY.",
                file=sys.stderr,
            )
            sys.exit(1)


def build_provider_candidates(provider: str) -> list[ProviderConfig]:
    """List the providers to try, in order, from the environment.

    A Claude subscription token comes before a billed Anthropic key, and OpenRouter
    comes last, so in `auto` mode it is the fallback.
    """
    claude_code_oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()

    # Resolve provider endpoints and models
    anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    anthropic_base_url = os.environ.get(
        "ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1/messages"
    )
    try:
        anthropic_max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "8192"))
    except ValueError:
        anthropic_max_tokens = 8192

    openrouter_model = os.environ.get("OPENROUTER_MODEL", "openrouter/free")
    openrouter_base_url = os.environ.get(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"
    )
    try:
        openrouter_max_tokens = int(os.environ.get("OPENROUTER_MAX_TOKENS", "8192"))
    except ValueError:
        openrouter_max_tokens = 8192

    # Build candidate providers based on configuration and available credentials
    candidates: list[ProviderConfig] = []
    if provider in ("auto", "anthropic"):
        # Claude Pro/Max subscription OAuth token takes precedence over billed API key
        if claude_code_oauth_token:
            candidates.append(
                ProviderConfig(
                    name="anthropic",
                    api_key=claude_code_oauth_token,
                    model=anthropic_model,
                    base_url=anthropic_base_url,
                    max_tokens=anthropic_max_tokens,
                    use_oauth=True,
                    auth_desc="Claude Pro/Max OAuth token",
                )
            )
        if anthropic_api_key:
            candidates.append(
                ProviderConfig(
                    name="anthropic",
                    api_key=anthropic_api_key,
                    model=anthropic_model,
                    base_url=anthropic_base_url,
                    max_tokens=anthropic_max_tokens,
                    use_oauth=False,
                    auth_desc="Anthropic API key",
                )
            )
    # OpenRouter candidate: used in auto mode as fallback/primary, or explicitly in openrouter mode
    if provider in ("auto", "openrouter"):
        if openrouter_api_key:
            candidates.append(
                ProviderConfig(
                    name="openrouter",
                    api_key=openrouter_api_key,
                    model=openrouter_model,
                    base_url=openrouter_base_url,
                    max_tokens=openrouter_max_tokens,
                    use_oauth=False,
                    auth_desc="OpenRouter API key",
                )
            )
    return candidates


T = TypeVar("T")


def request_with_fallback(
    candidates: list[ProviderConfig],
    send: Callable[[ProviderConfig], str],
    parse: Callable[[str], T],
    what: str = "review",
    max_attempts: int = 3,
) -> tuple[T | None, str, Exception | None]:
    """Ask each provider in turn until one returns something `parse` accepts.

    `send` makes one request to a provider and returns the reply text; `parse` turns
    it into a result and raises ValueError and its relatives if it cannot. A provider
    is retried up to `max_attempts` times. Moving on to the next one happens only for
    an error that is the provider's rather than the request's (see
    `is_token_or_server_error`): bad credentials, no credit, an overloaded or
    unreachable server. Any other failure is retried and then ends the run, because
    another provider would get the same request.

    Returns (result, raw_response, last_exception): `result` is None when every
    attempt failed; `raw_response` is the reply to the last attempt, empty if that
    attempt got no reply; `last_exception` is the last error seen.
    """
    raw_response = ""
    last_exception: Exception | None = None

    for provider_idx, config in enumerate(candidates):
        has_fallback = provider_idx + 1 < len(candidates)
        next_provider_name = candidates[provider_idx + 1].name if has_fallback else None
        print(
            f"Requesting {what} from provider '{config.name}' (auth: {config.auth_desc})..."
        )

        should_fallback = False
        for attempt in range(1, max_attempts + 1):
            raw_response = ""
            try:
                raw_response = send(config)
                return parse(raw_response), raw_response, last_exception
            except (
                ValueError,
                KeyError,
                TypeError,
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as e:
                last_exception = e
                # Check if this error is an authentication/token error or a 5xx/server error
                if is_token_or_server_error(e):
                    if has_fallback:
                        print(
                            f"Warning: Provider '{config.name}' ({config.auth_desc}) failed with "
                            f"token/server error: {log_text(e)}. Falling back to '{next_provider_name}'...",
                            file=sys.stderr,
                        )
                        should_fallback = True
                        break
                    else:
                        print(
                            f"Warning: Provider '{config.name}' ({config.auth_desc}) failed with "
                            f"token/server error: {log_text(e)}. No fallback provider available.",
                            file=sys.stderr,
                        )
                # For non-token/non-500 errors or when no fallback is configured, retry if attempts remain
                if attempt < max_attempts:
                    print(
                        f"Warning: {what.capitalize()} attempt {attempt} failed (error or malformed JSON): "
                        f"{log_text(e)}. "
                        "Retrying request...",
                        file=sys.stderr,
                    )
                    time.sleep(1.0 * attempt)

        if should_fallback:
            continue
        # Non-fallback error or attempts exhausted without fallback
        break

    return None, raw_response, last_exception
