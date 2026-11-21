"""Unit tests for the automated code review script."""

import json
import os
import random
import re
import shutil
import subprocess
import time
from collections.abc import Callable

import pytest
from unittest.mock import MagicMock, call, patch
from pr_review import (
    get_modified_lines,
    parse_llm_json,
    truncate_diff,
    annotate_diff,
    clean_markdown_line,
    parse_markdown_comments,
    filter_diff,
    get_exclude_regexes,
    MAX_COMMENT_CHARS,
    MAX_SUMMARY_CHARS,
    _FENCE_LANGUAGES,
    _MAX_GROWTH_FACTOR,
    _OUTPUT_SLACK,
    SanitizeError,
    sanitize_llm_text,
)


def test_get_modified_lines_single_file() -> None:
    diff_text = """diff --git a/src/main.py b/src/main.py
index 1234567..89abcde 100644
--- a/src/main.py
+++ b/src/main.py
@@ -10,6 +10,8 @@ def hello():
  def hello():
      print("hello")
-     print("old")
+     print("new")
+     print("another new")
      return True
"""
    modified = get_modified_lines(diff_text)
    assert "src/main.py" in modified
    # Added lines are 12 and 13 in the new file (10: def, 11: print hello, 12: new, 13: another new)
    assert modified["src/main.py"] == {12, 13}


def test_get_modified_lines_multiple_files() -> None:
    diff_text = """diff --git a/file1.py b/file1.py
--- a/file1.py
+++ b/file1.py
@@ -1,3 +1,4 @@
  line1
-line2
+ line2_mod
  line3
+ line4_add
diff --git a/file2.py b/file2.py
--- a/file2.py
+++ b/file2.py
@@ -5,2 +5,3 @@
  unchanged
+ added_line
"""
    modified = get_modified_lines(diff_text)
    assert "file1.py" in modified
    assert "file2.py" in modified
    # file1.py: lines 2 and 4 are modified/added
    assert modified["file1.py"] == {2, 4}
    # file2.py: line 6 is added (5: unchanged, 6: added_line)
    assert modified["file2.py"] == {6}


def test_get_modified_lines_deleted_file() -> None:
    diff_text = """diff --git a/deleted.py b/deleted.py
deleted file mode 100644
--- a/deleted.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line1
-line2
"""
    modified = get_modified_lines(diff_text)
    # The target file is /dev/null or deleted, shouldn't be counted as a new modified file
    assert "/dev/null" not in modified
    assert "deleted.py" not in modified


def test_parse_llm_json_clean() -> None:
    json_str = '{"summary": "Looks good", "comments": [{"path": "main.py", "line": 5, "body": "nice"}]}'
    result = parse_llm_json(json_str)
    assert result["summary"] == "Looks good"
    assert len(result["comments"]) == 1
    assert result["comments"][0]["line"] == 5


def test_parse_llm_json_wrapped() -> None:
    wrapped_str = """```json
{
  "summary": "wrapped",
  "comments": []
}
```"""
    result = parse_llm_json(wrapped_str)
    assert result["summary"] == "wrapped"
    assert result["comments"] == []


def test_truncate_diff() -> None:
    # Under limit: remains untouched
    assert truncate_diff("hello world", 50) == "hello world"

    # Over limit: truncates at last newline
    long_text = "line1\nline2\nline3\nline4"
    # limit of 15 lands on index 15 which is inside line3 ('line1\nline2\nline3')
    # last newline before 15 is index 11 ('\n' after line2)
    truncated = truncate_diff(long_text, 15)
    assert truncated.startswith("line1\nline2\n\n")
    assert "truncated" in truncated

    # Over limit: no newline before limit, fallback to hard truncation
    no_newline_text = "abcdefghijklmnop"
    truncated_no_nl = truncate_diff(no_newline_text, 5)
    assert truncated_no_nl.startswith("abcde\n\n")
    assert "truncated" in truncated_no_nl


@patch("pr_review.urllib.request.urlopen")
def test_send_request_success(mock_urlopen: MagicMock) -> None:
    import json
    import urllib.request
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = json.dumps(
        {"choices": [{"message": {"content": '{"summary": "OK"}'}}]}
    ).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_res

    content = _send_request(
        url="http://dummy",
        payload={"dummy": "data"},
        headers={"Authorization": "Bearer key"},
    )

    assert content == '{"summary": "OK"}'
    mock_urlopen.assert_called_once()
    args, kwargs = mock_urlopen.call_args
    assert isinstance(args[0], urllib.request.Request)
    assert kwargs.get("timeout") == 30


@patch("pr_review.urllib.request.urlopen")
def test_send_request_empty_choices(mock_urlopen: MagicMock) -> None:
    import pytest
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = b'{"choices": []}'
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(ValueError, match="OpenRouter returned empty choices"):
        _send_request("http://dummy", {}, {})


@patch("pr_review.urllib.request.urlopen")
def test_send_request_non_string_content(mock_urlopen: MagicMock) -> None:
    import pytest
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = b'{"choices": [{"message": {"content": 123}}]}'
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(
        ValueError, match="OpenRouter returned non-string message content"
    ):
        _send_request("http://dummy", {}, {})


@patch("pr_review.urllib.request.urlopen")
def test_send_request_error_response(mock_urlopen: MagicMock) -> None:
    import pytest
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = (
        b'{"error": {"message": "Model not found", "code": 404}}'
    )
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(ValueError, match="OpenRouter Error: Model not found"):
        _send_request("http://dummy", {}, {})


@patch("pr_review.urllib.request.urlopen")
def test_send_request_null_content_refusal(mock_urlopen: MagicMock) -> None:
    import pytest
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = b'{"choices": [{"message": {"content": null, "refusal": "I cannot review this"}, "finish_reason": "refusal"}]}'
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(ValueError, match="OpenRouter Refusal: I cannot review this"):
        _send_request("http://dummy", {}, {})


@patch("pr_review.urllib.request.urlopen")
def test_send_request_null_content_no_refusal(mock_urlopen: MagicMock) -> None:
    import pytest
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = b'{"choices": [{"message": {"content": null}}]}'
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(
        ValueError, match="OpenRouter returned message with null content"
    ):
        _send_request("http://dummy", {}, {})


@patch("pr_review._send_request")
def test_make_openrouter_request_success(mock_send_request: MagicMock) -> None:
    from pr_review import make_openrouter_request

    mock_send_request.return_value = '{"summary": "Review complete"}'

    result = make_openrouter_request("fake_key", "fake_diff")

    assert result == '{"summary": "Review complete"}'
    mock_send_request.assert_called_once()
    args, kwargs = mock_send_request.call_args
    payload = args[1]
    assert payload["response_format"] == {"type": "json_object"}


@patch("pr_review._send_request")
def test_make_openrouter_request_comments_only(
    mock_send_request: MagicMock,
) -> None:
    from pr_review import make_openrouter_request

    mock_send_request.return_value = '{"comments": []}'

    result = make_openrouter_request("fake_key", "fake_diff", post_summary=False)

    assert result == '{"comments": []}'
    mock_send_request.assert_called_once()
    args, kwargs = mock_send_request.call_args
    payload = args[1]
    assert payload["response_format"] == {"type": "json_object"}
    system_msg = payload["messages"][0]["content"]
    # Check that it uses the simplified instruction file
    assert "comments" in system_msg
    assert "thinking" not in system_msg


@patch("pr_review._send_request")
def test_make_openrouter_request_fallback_success(mock_send_request: MagicMock) -> None:
    from pr_review import make_openrouter_request

    mock_send_request.side_effect = [
        Exception("HTTP Error 400: Bad Request"),
        '{"summary": "Fallback works"}',
    ]

    result = make_openrouter_request("fake_key", "fake_diff")

    assert result == '{"summary": "Fallback works"}'
    assert mock_send_request.call_count == 2

    first_call_payload = mock_send_request.call_args_list[0][0][1]
    assert "response_format" in first_call_payload

    second_call_payload = mock_send_request.call_args_list[1][0][1]
    assert "response_format" not in second_call_payload


@patch("pr_review._send_request")
def test_make_openrouter_request_fallback_failure(mock_send_request: MagicMock) -> None:
    import pytest
    from pr_review import make_openrouter_request

    mock_send_request.side_effect = [
        Exception("HTTP Error 400: Bad Request"),
        Exception("Fallback failed"),
    ]

    with pytest.raises(Exception, match="Fallback failed"):
        make_openrouter_request("fake_key", "fake_diff")
    assert mock_send_request.call_count == 2


@patch("pr_review._send_request")
def test_make_openrouter_request_no_fallback_on_unauthorized(
    mock_send_request: MagicMock,
) -> None:
    import pytest
    from pr_review import make_openrouter_request

    mock_send_request.side_effect = Exception("HTTP Error 401: Unauthorized")

    with pytest.raises(Exception, match="HTTP Error 401: Unauthorized"):
        make_openrouter_request("fake_key", "fake_diff")
    assert mock_send_request.call_count == 1


@patch("pr_review._send_request")
def test_make_openrouter_request_fallback_on_value_error(
    mock_send_request: MagicMock,
) -> None:
    from pr_review import make_openrouter_request

    mock_send_request.side_effect = [
        ValueError("OpenRouter returned message with null content"),
        '{"summary": "Fallback works after value error"}',
    ]

    result = make_openrouter_request("fake_key", "fake_diff")

    assert result == '{"summary": "Fallback works after value error"}'
    assert mock_send_request.call_count == 2


def test_annotate_diff() -> None:
    diff_text = """diff --git a/src/main.py b/src/main.py
index 1234567..89abcde 100644
--- a/src/main.py
+++ b/src/main.py
@@ -10,6 +10,8 @@ def hello():
  def hello():
      print("hello")
-     print("old")
+     print("new")
+     print("another new")
      return True
"""
    annotated = annotate_diff(diff_text)
    lines = annotated.splitlines()
    assert "+++ b/src/main.py" in lines
    assert "@@ -10,6 +10,8 @@ def hello():" in lines
    assert any(line.endswith(': -     print("old")') for line in lines)
    assert "   10:   def hello():" in lines
    assert '   11:       print("hello")' in lines
    assert '   12: +     print("new")' in lines
    assert '   13: +     print("another new")' in lines
    assert "   14:       return True" in lines


def test_filter_diff() -> None:
    diff_text = """diff --git a/src/main.py b/src/main.py
index 123..456 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,2 +1,2 @@
-hello
+world
diff --git a/package-lock.json b/package-lock.json
index abc..def 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,2 +1,3 @@
 {
+  "version": "1.0.1"
 }
diff --git "a/src/my file.min.js" "b/src/my file.min.js"
index 789..012 100644
--- a/src/my file.min.js
+++ b/src/my file.min.js
@@ -1,1 +1,1 @@
-console.log("hello");
+console.log("world");
diff --git a/logo.svg b/logo.svg
index 345..678 100644
--- a/logo.svg
+++ b/logo.svg
@@ -1,1 +1,1 @@
-<svg></svg>
+<svg viewBox="0 0 100 100"></svg>
"""
    exclude_regexes = get_exclude_regexes()
    filtered = filter_diff(diff_text, exclude_regexes)
    assert "src/main.py" in filtered
    assert "package-lock.json" not in filtered
    assert "my file.min.js" not in filtered
    assert "logo.svg" not in filtered


def test_get_modified_lines_quoted_path() -> None:
    diff_text = """diff --git a/src/my file.py b/src/my file.py
--- a/src/my file.py
+++ "b/src/my file.py"
@@ -1,2 +1,3 @@
  hello
+ world
"""
    modified = get_modified_lines(diff_text)
    assert "src/my file.py" in modified
    assert modified["src/my file.py"] == {2}


def test_parse_llm_json_trailing_commas() -> None:
    json_str = """
    {
        "summary": "OK",
        "comments": [
            {
                "path": "main.py",
                "line": 5,
                "body": "test comma",
            },
        ],
    }
    """
    result = parse_llm_json(json_str)
    assert result["summary"] == "OK"
    assert len(result["comments"]) == 1
    assert result["comments"][0]["line"] == 5


def test_parse_llm_json_unescaped_control_chars() -> None:
    # A JSON string containing a literal (raw) newline inside a string value.
    raw_newline_json = '{"summary": "Line 1\nLine 2", "comments": []}'
    result = parse_llm_json(raw_newline_json)
    assert result["summary"] == "Line 1\nLine 2"


def test_clean_markdown_line() -> None:
    assert clean_markdown_line("### src/app.rs") == "src/app.rs"
    assert clean_markdown_line("In src/app.rs:") == "In src/app.rs:"
    assert clean_markdown_line("**src/app.rs**") == "src/app.rs"
    assert clean_markdown_line("`src/app.rs`") == "src/app.rs"
    assert clean_markdown_line("   ###  `src/app.rs`  ") == "src/app.rs"
    assert clean_markdown_line("- In src/app.rs:") == "In src/app.rs:"
    assert clean_markdown_line("* In src/app.rs:") == "In src/app.rs:"
    assert clean_markdown_line("+ In src/app.rs:") == "In src/app.rs:"
    assert clean_markdown_line("1. In src/app.rs:") == "In src/app.rs:"
    assert clean_markdown_line("12. In src/app.rs:") == "In src/app.rs:"


def test_parse_markdown_comments() -> None:
    modified_lines = {
        "src/app.rs": {81, 103, 112},
        "src/fs.rs": {42, 43, 44},
    }

    markdown_text = """
    We will go through the passes.

    - In APP.RS:
     - Line 81: `current_dir: start_path.canonicalize()`
       This is acceptable because...
       It should handle error.

     - Lines 103: self.entries = list_dir()
       This is okay.

    ### Review for Fs.rs
    - Line 42-44: split_name_ext
      This is a good helper function.
    """

    comments = parse_markdown_comments(markdown_text, modified_lines)
    assert len(comments) == 3

    c1 = comments[0]
    assert c1["path"] == "src/app.rs"
    assert c1["line"] == 81
    assert "This is acceptable because" in c1["body"]
    assert "It should handle error." in c1["body"]

    c2 = comments[1]
    assert c2["path"] == "src/app.rs"
    assert c2["line"] == 103
    assert c2["body"] == "self.entries = list_dir()\n       This is okay."

    c3 = comments[2]
    assert c3["path"] == "src/fs.rs"
    assert c3["line"] == 44
    assert "This is a good helper function." in c3["body"]


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_retry_on_429_then_success(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    import urllib.error
    from pr_review import _send_request

    # First attempt raises HTTPError 429
    mock_headers = MagicMock()
    mock_headers.get.return_value = "5"  # Retry-After header

    # We need an HTTPError mock
    err_body = b'{"error": {"message": "Rate limited", "metadata": {"retry_after_seconds": 10}}}'
    fp = MagicMock()
    fp.read.return_value = err_body

    http_err = urllib.error.HTTPError(
        "http://dummy", 429, "Too Many Requests", mock_headers, fp
    )

    # Second attempt succeeds
    mock_res = MagicMock()
    mock_res.__enter__.return_value = mock_res
    mock_res.read.return_value = b'{"choices": [{"message": {"content": "Success"}}]}'

    # Set side effect
    mock_urlopen.side_effect = [http_err, mock_res]

    content = _send_request("http://dummy", {}, {})
    assert content == "Success"
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once()
    # Check that sleep duration was at least the retry_after value
    sleep_args, _ = mock_sleep.call_args
    assert sleep_args[0] >= 10.0


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_retry_on_network_error_then_success(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    from pr_review import _send_request
    import urllib.error

    # First attempt raises URLError
    url_err = urllib.error.URLError("DNS resolution failed")

    # Second attempt succeeds
    mock_res = MagicMock()
    mock_res.__enter__.return_value = mock_res
    mock_res.read.return_value = (
        b'{"choices": [{"message": {"content": "Success Network"}}]}'
    )

    mock_urlopen.side_effect = [url_err, mock_res]

    content = _send_request("http://dummy", {}, {})
    assert content == "Success Network"
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once()


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_rate_limit_in_json_body_then_success(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    from pr_review import _send_request

    # First attempt returns 200 OK but with error in JSON payload
    mock_res_err = MagicMock()
    mock_res_err.__enter__.return_value = mock_res_err
    mock_res_err.read.return_value = b'{"error": {"message": "Rate limit reached", "code": 429, "metadata": {"retry_after_seconds": 3}}}'

    # Second attempt succeeds
    mock_res_ok = MagicMock()
    mock_res_ok.__enter__.return_value = mock_res_ok
    mock_res_ok.read.return_value = (
        b'{"choices": [{"message": {"content": "Success JSON"}}]}'
    )

    mock_urlopen.side_effect = [mock_res_err, mock_res_ok]

    content = _send_request("http://dummy", {}, {})
    assert content == "Success JSON"
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once()
    sleep_args, _ = mock_sleep.call_args
    assert sleep_args[0] >= 3.0


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_max_retries_exhausted(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    import pytest
    import urllib.error
    from pr_review import _send_request

    # Always raise URLError
    mock_urlopen.side_effect = urllib.error.URLError("Connection timed out")

    with pytest.raises(urllib.error.URLError, match="Connection timed out"):
        _send_request("http://dummy", {}, {})

    # 1 initial attempt + 5 retries = 6 calls total
    assert mock_urlopen.call_count == 6
    assert mock_sleep.call_count == 5


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_stale_error_body_bug(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    import pytest
    import urllib.error
    from pr_review import _send_request

    # First attempt raises HTTPError 429 (retryable)
    mock_headers_429 = MagicMock()
    fp_429 = MagicMock()
    fp_429.read.return_value = b"Rate limit details"
    err_429 = urllib.error.HTTPError(
        "http://dummy", 429, "Too Many Requests", mock_headers_429, fp_429
    )

    # Second attempt raises HTTPError 400 (non-retryable)
    mock_headers_400 = MagicMock()
    fp_400 = MagicMock()
    fp_400.read.return_value = b"Bad Request details"
    err_400 = urllib.error.HTTPError(
        "http://dummy", 400, "Bad Request", mock_headers_400, fp_400
    )

    mock_urlopen.side_effect = [err_429, err_400]

    with pytest.raises(ValueError) as excinfo:
        _send_request("http://dummy", {}, {})

    assert "HTTP Error 400: Bad Request details" in str(excinfo.value)


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_retry_on_503_then_success(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    import urllib.error
    from pr_review import _send_request

    # First attempt raises HTTPError 503 (retryable)
    mock_headers = MagicMock()
    fp = MagicMock()
    fp.read.return_value = b"Service Unavailable"
    http_err_503 = urllib.error.HTTPError(
        "http://dummy", 503, "Service Unavailable", mock_headers, fp
    )

    # Second attempt succeeds
    mock_res = MagicMock()
    mock_res.__enter__.return_value = mock_res
    mock_res.read.return_value = (
        b'{"choices": [{"message": {"content": "Success 503"}}]}'
    )

    mock_urlopen.side_effect = [http_err_503, mock_res]

    content = _send_request("http://dummy", {}, {})
    assert content == "Success 503"
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once()


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_retry_after_header_only_then_success(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    import urllib.error
    from pr_review import _send_request

    # First attempt raises HTTPError 429 with Retry-After header, no body metadata
    mock_headers = MagicMock()
    mock_headers.get.side_effect = lambda name, default=None: (
        "15" if name == "Retry-After" else default
    )
    fp = MagicMock()
    fp.read.return_value = b""  # empty/invalid JSON body
    http_err = urllib.error.HTTPError(
        "http://dummy", 429, "Too Many Requests", mock_headers, fp
    )

    # Second attempt succeeds
    mock_res = MagicMock()
    mock_res.__enter__.return_value = mock_res
    mock_res.read.return_value = (
        b'{"choices": [{"message": {"content": "Success Header"}}]}'
    )

    mock_urlopen.side_effect = [http_err, mock_res]

    content = _send_request("http://dummy", {}, {})
    assert content == "Success Header"
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once()
    sleep_args, _ = mock_sleep.call_args
    assert sleep_args[0] >= 15.0


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_retry_after_invalid_header_then_success(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    import urllib.error
    from pr_review import _send_request

    # First attempt raises HTTPError 429 with invalid Retry-After header
    mock_headers = MagicMock()
    mock_headers.get.side_effect = lambda name, default=None: (
        "invalid_delay" if name == "Retry-After" else default
    )
    fp = MagicMock()
    fp.read.return_value = b""
    http_err = urllib.error.HTTPError(
        "http://dummy", 429, "Too Many Requests", mock_headers, fp
    )

    # Second attempt succeeds
    mock_res = MagicMock()
    mock_res.__enter__.return_value = mock_res
    mock_res.read.return_value = (
        b'{"choices": [{"message": {"content": "Success Invalid Header"}}]}'
    )

    mock_urlopen.side_effect = [http_err, mock_res]

    content = _send_request("http://dummy", {}, {})
    assert content == "Success Invalid Header"
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once()
    sleep_args, _ = mock_sleep.call_args
    assert sleep_args[0] >= 1.0  # base_delay is 1.0


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_http_error_exhausted(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    import pytest
    import urllib.error
    from pr_review import _send_request

    # Always raise HTTPError 500
    mock_headers = MagicMock()
    fp = MagicMock()
    fp.read.return_value = b"Internal Server Error"
    http_err = urllib.error.HTTPError(
        "http://dummy", 500, "Internal Server Error", mock_headers, fp
    )
    mock_urlopen.side_effect = http_err

    with pytest.raises(ValueError) as excinfo:
        _send_request("http://dummy", {}, {})

    assert "OpenRouter HTTP Error 500: Internal Server Error" in str(excinfo.value)
    # 1 initial + 5 retries = 6 attempts total
    assert mock_urlopen.call_count == 6
    assert mock_sleep.call_count == 5


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_timeout_error_then_success(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    from pr_review import _send_request

    # First attempt raises TimeoutError
    time_err = TimeoutError("Request timed out")

    # Second attempt succeeds
    mock_res = MagicMock()
    mock_res.__enter__.return_value = mock_res
    mock_res.read.return_value = (
        b'{"choices": [{"message": {"content": "Success Timeout"}}]}'
    )

    mock_urlopen.side_effect = [time_err, mock_res]

    content = _send_request("http://dummy", {}, {})
    assert content == "Success Timeout"
    assert mock_urlopen.call_count == 2
    mock_sleep.assert_called_once()


@patch.dict(
    "os.environ",
    {
        "OPENROUTER_API_KEY": "fake_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
)
@patch("time.sleep")
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.submit_github_review")
def test_main_fallback_summary_on_json_parse_failure(
    mock_submit: MagicMock,
    mock_make_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
    mock_sleep: MagicMock,
) -> None:
    """Verify that if the LLM output fails to parse as a JSON object, the action is resilient,

    does not crash, and submits a generic warning message pointing developers to logs.
    """
    from pr_review import main

    mock_exists.return_value = True

    # Mock reading the diff file
    mock_file = MagicMock()
    mock_file.read.return_value = "diff --git a/test.py b/test.py\n..."
    mock_open.return_value.__enter__.return_value = mock_file

    # Mock LLM returning free-form markdown (so JSON parsing fails)
    mock_make_req.return_value = "This is a free-form raw LLM response with no JSON."

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1

    assert mock_sleep.call_count == 2
    mock_submit.assert_called_once()
    args = mock_submit.call_args[0]
    # args: (repo, pr_number, token, final_body, valid_comments)
    assert args[0] == "owner/repo"
    assert args[1] == "12"
    assert args[2] == "fake_github_token"
    # Verify final_body contains fallback warning but does NOT contain raw prompt-injection risk output
    assert "could not be parsed as a JSON object" in args[3]
    assert "workflow execution logs" in args[3]
    assert "This is a free-form raw LLM response with no JSON." not in args[3]
    # Verify comments list is empty
    assert args[4] == []


@patch.dict(
    "os.environ",
    {
        "OPENROUTER_API_KEY": "fake_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
)
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.submit_github_review")
def test_main_success_json_parsed(
    mock_submit: MagicMock,
    mock_make_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Verify the happy-path behavior: when the LLM returns a valid JSON response containing

    both a summary and inline comments, the review is successfully parsed and submitted to GitHub.
    """
    from pr_review import main

    mock_exists.return_value = True

    # Mock reading the diff file with an added line 5
    mock_file = MagicMock()
    mock_file.read.return_value = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,5 +1,5 @@
 line1
 line2
 line3
 line4
+line5
"""
    mock_open.return_value.__enter__.return_value = mock_file

    # Mock LLM returning valid JSON
    json_response = {
        "summary": "Looks good!",
        "comments": [{"path": "test.py", "line": 5, "body": "Nice change"}],
    }
    mock_make_req.return_value = json.dumps(json_response)

    main()

    mock_submit.assert_called_once()
    args = mock_submit.call_args[0]
    assert args[4][0]["side"] == "RIGHT"


def test_filter_diff_more_edge_cases() -> None:
    """Verify filter_diff edge cases, including unquoted spaces, quotes, and metadata headers."""
    # Diff containing spaces in a non-excluded file and arbitrary pre-diff header text
    diff_text = """some initial git metadata headers
and arbitrary text
diff --git "a/src/my file.py" "b/src/my file.py"
index 123..456 100644
--- "a/src/my file.py"
+++ "b/src/my file.py"
@@ -1,2 +1,2 @@
-hello
+world
diff --git a/package-lock.json b/package-lock.json
index abc..def 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,2 +1,3 @@
 {
+  "version": "1.0.1"
 }
"""
    exclude_regexes = get_exclude_regexes()
    filtered = filter_diff(diff_text, exclude_regexes)

    # Pre-diff headers should be preserved
    assert "some initial git metadata headers" in filtered
    # Non-excluded file with spaces and quotes should be preserved
    assert "src/my file.py" in filtered
    # Excluded file should be removed
    assert "package-lock.json" not in filtered


def test_get_exclude_regexes() -> None:
    """Verify that get_exclude_regexes merges custom patterns correctly."""
    regexes = get_exclude_regexes("my-custom-pattern\\.txt$,another-pattern.*")
    # Verify defaults are present
    assert any(rx.search("package-lock.json") for rx in regexes)
    # Verify custom patterns are present
    assert any(rx.search("my-custom-pattern.txt") for rx in regexes)
    assert any(rx.search("another-pattern.log") for rx in regexes)


@patch("os.path.exists")
def test_main_missing_env_vars(mock_exists: MagicMock) -> None:
    """Verify that main() exits with 1 when required env vars are missing or invalid."""
    from pr_review import main

    # 1. Missing API Key
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1

    # 2. Missing other required vars
    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake_key"}, clear=True):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1

    # 3. Invalid numeric PR_NUMBER
    with patch.dict(
        "os.environ",
        {
            "OPENROUTER_API_KEY": "fake_key",
            "PR_NUMBER": "abc",
            "REPO": "owner/repo",
            "GH_TOKEN": "fake_token",
        },
        clear=True,
    ):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1

    # 4. Invalid REPO format
    with patch.dict(
        "os.environ",
        {
            "OPENROUTER_API_KEY": "fake_key",
            "PR_NUMBER": "12",
            "REPO": "invalid-repo-format",
            "GH_TOKEN": "fake_token",
        },
        clear=True,
    ):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1


@patch("os.path.exists")
@patch("builtins.open")
def test_main_empty_and_missing_diff(
    mock_open: MagicMock, mock_exists: MagicMock
) -> None:
    """Verify main() handles missing and empty diff files correctly."""
    from pr_review import main

    base_env = {
        "OPENROUTER_API_KEY": "fake_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_token",
    }

    # 1. Missing diff file (exists=False) -> exits with 1
    mock_exists.return_value = False
    with patch.dict("os.environ", base_env, clear=True):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1

    # 2. Empty diff file -> exits with 0
    mock_exists.return_value = True
    mock_file = MagicMock()
    mock_file.read.return_value = ""
    mock_open.return_value.__enter__.return_value = mock_file
    with patch.dict("os.environ", base_env, clear=True):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 0

    # 3. Diff containing only excluded files -> exits with 0
    mock_file.read.return_value = """diff --git a/package-lock.json b/package-lock.json
index abc..def 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,2 +1,3 @@
 {
+  "version": "1.0.1"
 }
"""
    with patch.dict("os.environ", base_env, clear=True):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 0


@patch("pr_review.urllib.request.urlopen")
def test_submit_github_review_success(mock_urlopen: MagicMock) -> None:
    """Verify submit_github_review constructs headers, payload, and successfully submits."""
    from pr_review import submit_github_review
    import urllib.request

    mock_res = MagicMock()
    mock_res.read.return_value = b'{"id": 123, "state": "COMMENTED"}'
    mock_urlopen.return_value.__enter__.return_value = mock_res

    res = submit_github_review(
        repo="owner/repo",
        pr_number="12",
        token="fake_token",
        summary="A review summary",
        comments=[{"path": "test.py", "line": 5, "side": "RIGHT", "body": "Nice"}],
    )

    assert res["id"] == 123
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert isinstance(req, urllib.request.Request)
    assert req.full_url == "https://api.github.com/repos/owner/repo/pulls/12/reviews"
    assert req.headers["Authorization"] == "token fake_token"
    assert req.headers["Accept"] == "application/vnd.github.v3+json"


@patch("pr_review.urllib.request.urlopen")
def test_submit_github_review_error(mock_urlopen: MagicMock) -> None:
    """Verify submit_github_review handles HTTPError correctly."""
    from pr_review import submit_github_review
    import urllib.error

    fp = MagicMock()
    fp.read.return_value = b"Invalid request details"
    http_err = urllib.error.HTTPError(
        "http://dummy", 400, "Bad Request", MagicMock(), fp
    )
    mock_urlopen.side_effect = http_err

    with pytest.raises(urllib.error.HTTPError):
        submit_github_review("owner/repo", "12", "fake_token", "summary", [])


@patch.dict(
    "os.environ",
    {
        "OPENROUTER_API_KEY": "fake_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "false",
    },
)
@patch("time.sleep")
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.submit_github_review")
def test_main_fallback_warning_on_json_parse_failure_post_summary_false(
    mock_submit: MagicMock,
    mock_make_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
    mock_sleep: MagicMock,
) -> None:
    """Verify that if the LLM output fails to parse as a JSON object and POST_SUMMARY is False,

    the action still submits the fallback warning message instead of 'No issues found'.
    """
    from pr_review import main

    mock_exists.return_value = True

    # Mock reading the diff file
    mock_file = MagicMock()
    mock_file.read.return_value = "diff --git a/test.py b/test.py\n..."
    mock_open.return_value.__enter__.return_value = mock_file

    # Mock LLM returning free-form markdown (so JSON parsing fails)
    mock_make_req.return_value = "This is a free-form raw LLM response with no JSON."

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1

    assert mock_sleep.call_count == 2
    mock_submit.assert_called_once()
    args = mock_submit.call_args[0]
    # Verify final_body contains the fallback warning instead of 'No issues found'
    assert "could not be parsed as a JSON object" in args[3]
    assert "workflow execution logs" in args[3]
    assert "No issues found" not in args[3]


def test_parse_llm_json_with_thinking() -> None:
    """Verify parse_llm_json correctly strips <think>...</think> blocks case-insensitively."""
    thinking_str = """<think>
Some thoughts about code review.
Let's mock JSON:
{
  "dummy": "this is a fake json inside think block"
}
Okay, now the real JSON:
</think>
{
  "summary": "real output",
  "comments": []
}"""
    result = parse_llm_json(thinking_str)
    assert result["summary"] == "real output"
    assert result["comments"] == []

    # Test lowercase <think>
    thinking_str_lower = """<think>
thoughts
</think>
{
  "summary": "real output lower",
  "comments": []
}"""
    result_lower = parse_llm_json(thinking_str_lower)
    assert result_lower["summary"] == "real output lower"

    # Test uppercase <Think>
    thinking_str_upper = """<Think>
thoughts
</THINK>
{
  "summary": "real output upper",
  "comments": []
}"""
    result_upper = parse_llm_json(thinking_str_upper)
    assert result_upper["summary"] == "real output upper"

    # Test unclosed <think> tag (e.g. truncated response)
    thinking_str_unclosed = """<think>
thoughts with curly { braces } inside it
and some more text
{
  "summary": "real output unclosed",
  "comments": []
}"""
    result_unclosed = parse_llm_json(thinking_str_unclosed)
    assert result_unclosed["summary"] == "real output unclosed"

    # Test that <think> tags nested inside JSON string values are preserved and NOT stripped
    nested_str = (
        '{"summary": "Review for <think>tags</think> in comments.", "comments": []}'
    )
    result_nested = parse_llm_json(nested_str)
    assert result_nested["summary"] == "Review for <think>tags</think> in comments."


def test_parse_llm_json_preamble_extraction() -> None:
    """Verify parse_llm_json extracts valid JSON when surrounded by text and extra braces."""
    text = """Here is some preamble with a {dummy} brace.
    {
        "summary": "success",
        "comments": []
    }
    And some trailing text."""
    result = parse_llm_json(text)
    assert result["summary"] == "success"
    assert result["comments"] == []


def test_calculate_backoff() -> None:
    """Verify that _calculate_backoff computes delay correctly, respects retry_after, and caps at 60.0."""
    from pr_review import _calculate_backoff

    # 1. Exponential increase with attempts
    delay_0 = _calculate_backoff(attempt=0, base_delay=1.0)
    delay_1 = _calculate_backoff(attempt=1, base_delay=1.0)
    delay_2 = _calculate_backoff(attempt=2, base_delay=1.0)

    # Allow for jitter (0.1 to 1.0 seconds)
    # base_delay * (2**0) + jitter => 1.0 + [0.1, 1.0] => [1.1, 2.0]
    # base_delay * (2**1) + jitter => 2.0 + [0.1, 1.0] => [2.1, 3.0]
    # base_delay * (2**2) + jitter => 4.0 + [0.1, 1.0] => [4.1, 5.0]
    assert 1.1 <= delay_0 <= 2.0
    assert 2.1 <= delay_1 <= 3.0
    assert 4.1 <= delay_2 <= 5.0

    # 2. respects retry_after parameter if it is larger than computed backoff
    delay_ra_large = _calculate_backoff(attempt=1, base_delay=1.0, retry_after=10.0)
    assert delay_ra_large == 10.0

    # retry_after is ignored if it is smaller than computed backoff
    delay_ra_small = _calculate_backoff(attempt=2, base_delay=1.0, retry_after=1.0)
    assert 4.1 <= delay_ra_small <= 5.0

    # 3. capped at 60.0 seconds
    delay_capped = _calculate_backoff(attempt=10, base_delay=1.0)
    # 1.0 * (2**10) = 1024. But it should be capped at 60.0
    assert delay_capped == 60.0

    # custom retry_after larger than 60.0 is also capped at 60.0
    delay_capped_ra = _calculate_backoff(attempt=1, base_delay=1.0, retry_after=120.0)
    assert delay_capped_ra == 60.0


@patch("pr_review._send_request")
def test_make_openrouter_request_reasoning_exclude(
    mock_send_request: MagicMock,
) -> None:
    """Verify that make_openrouter_request includes or excludes 'reasoning' based on base_url."""
    from pr_review import make_openrouter_request

    # 1. Base URL is OpenRouter -> includes reasoning: exclude: True
    mock_send_request.return_value = '{"summary": "Review complete"}'
    make_openrouter_request(
        "fake_key",
        "fake_diff",
        base_url="https://openrouter.ai/api/v1/chat/completions",
    )
    mock_send_request.assert_called_once()
    payload = mock_send_request.call_args[0][1]
    assert payload["reasoning"] == {"exclude": True}

    # 2. Base URL is non-OpenRouter -> does NOT include reasoning config
    mock_send_request.reset_mock()
    make_openrouter_request(
        "fake_key", "fake_diff", base_url="https://api.openai.com/v1/chat/completions"
    )
    mock_send_request.assert_called_once()
    payload = mock_send_request.call_args[0][1]
    assert "reasoning" not in payload


@patch("pr_review._send_request")
def test_make_openrouter_request_fallback_removes_reasoning(
    mock_send_request: MagicMock,
) -> None:
    """Verify that make_openrouter_request fallback removes 'reasoning' from the retry payload."""
    from pr_review import make_openrouter_request

    # Side effect: first call raises ValueError (triggering fallback), second call succeeds
    mock_send_request.side_effect = [
        ValueError("First call failed"),
        '{"summary": "Fallback works"}',
    ]

    result = make_openrouter_request(
        "fake_key",
        "fake_diff",
        base_url="https://openrouter.ai/api/v1/chat/completions",
    )

    assert result == '{"summary": "Fallback works"}'
    assert mock_send_request.call_count == 2

    # Second call payload should have both 'response_format' and 'reasoning' popped
    second_call_payload = mock_send_request.call_args_list[1][0][1]
    assert "response_format" not in second_call_payload
    assert "reasoning" not in second_call_payload


def test_parse_llm_json_unescaped_quotes() -> None:
    """Verify parse_llm_json correctly repairs and parses unescaped double quotes inside key values."""
    # JSON with unescaped double quotes around "never drift" inside "body"
    raw_broken = '{ "summary": "Review summary", "comments": [ { "path": "test.py", "line": 5, "body": "guarantee logic \\"never drift\\", these checks" } ] }'.replace(
        '\\"', '"'
    )

    result = parse_llm_json(raw_broken)
    assert (
        result["comments"][0]["body"] == 'guarantee logic "never drift", these checks'
    )

    # Test with double quotes preceded by even number of backslashes (representing an escaped backslash followed by an unescaped quote)
    # The JSON string literal: "This has a backslash \\ and an unescaped quote \"never drift\""
    even_backslash_json = '{ "summary": "Review summary", "comments": [ { "path": "test.py", "line": 5, "body": "This has a backslash \\\\ and an unescaped quote \\\\"never drift\\\\"" } ] }'
    result_even = parse_llm_json(even_backslash_json)
    assert (
        result_even["comments"][0]["body"]
        == 'This has a backslash \\ and an unescaped quote \\"never drift\\"'
    )


def test_parse_llm_json_truncated() -> None:
    """Verify parse_llm_json correctly repairs and parses truncated JSON."""
    truncated_str = """{
  "thinking": "Pass 1: ...",
  "summary": "This is a block. The identical 14-line block"""
    result = parse_llm_json(truncated_str)
    assert result["summary"] == "This is a block. The identical 14-line block"
    assert "comments" not in result

    # Test backslash outside of JSON string values (e.g., in a preamble)
    backslash_outside_json = 'Here is a backslash: \\ and then the JSON: { "summary": "valid summary with unescaped quote \\"never drift\\"" }'
    result_outside = parse_llm_json(backslash_outside_json)
    assert (
        result_outside["summary"] == 'valid summary with unescaped quote "never drift"'
    )


# ---------------------------------------------------------------------------
# Anthropic (Messages API) provider tests
# ---------------------------------------------------------------------------


@patch("pr_review.urllib.request.urlopen")
def test_send_request_anthropic_success(mock_urlopen: MagicMock) -> None:
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = json.dumps(
        {
            "content": [{"type": "text", "text": '{"summary": "OK"}'}],
            "stop_reason": "end_turn",
        }
    ).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_res

    content = _send_request(
        url="http://dummy",
        payload={"dummy": "data"},
        headers={"x-api-key": "key"},
        provider="anthropic",
    )
    assert content == '{"summary": "OK"}'


@patch("pr_review.urllib.request.urlopen")
def test_send_request_anthropic_concatenates_text_blocks(
    mock_urlopen: MagicMock,
) -> None:
    from pr_review import _send_request

    # Non-text blocks (e.g. thinking) are ignored; text blocks are concatenated in order.
    mock_res = MagicMock()
    mock_res.read.return_value = json.dumps(
        {
            "content": [
                {"type": "thinking", "thinking": "reasoning..."},
                {"type": "text", "text": '{"summary":'},
                {"type": "text", "text": ' "joined"}'},
            ],
            "stop_reason": "end_turn",
        }
    ).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_res

    content = _send_request("http://dummy", {}, {}, provider="anthropic")
    assert content == '{"summary": "joined"}'


@patch("pr_review.urllib.request.urlopen")
def test_send_request_anthropic_error_body(mock_urlopen: MagicMock) -> None:
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = (
        b'{"type": "error", "error": {"type": "invalid_request_error",'
        b' "message": "model not found"}}'
    )
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(ValueError, match="Anthropic Error: model not found"):
        _send_request("http://dummy", {}, {}, provider="anthropic")


@patch("pr_review.urllib.request.urlopen")
def test_send_request_anthropic_refusal(mock_urlopen: MagicMock) -> None:
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = b'{"content": [], "stop_reason": "refusal"}'
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(ValueError, match="Anthropic refused to process"):
        _send_request("http://dummy", {}, {}, provider="anthropic")


@patch("pr_review.urllib.request.urlopen")
def test_send_request_anthropic_empty_content(mock_urlopen: MagicMock) -> None:
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = b'{"content": [], "stop_reason": "end_turn"}'
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(ValueError, match="Anthropic returned empty content"):
        _send_request("http://dummy", {}, {}, provider="anthropic")


@patch("pr_review.urllib.request.urlopen")
def test_send_request_anthropic_no_text_block(mock_urlopen: MagicMock) -> None:
    from pr_review import _send_request

    mock_res = MagicMock()
    mock_res.read.return_value = (
        b'{"content": [{"type": "thinking", "thinking": "..."}],'
        b' "stop_reason": "end_turn"}'
    )
    mock_urlopen.return_value.__enter__.return_value = mock_res

    with pytest.raises(ValueError, match="Anthropic returned no text content"):
        _send_request("http://dummy", {}, {}, provider="anthropic")


@patch("time.sleep")
@patch("pr_review.urllib.request.urlopen")
def test_send_request_anthropic_http_error(
    mock_urlopen: MagicMock, mock_sleep: MagicMock
) -> None:
    import urllib.error
    from pr_review import _send_request

    mock_headers = MagicMock()
    fp = MagicMock()
    fp.read.return_value = b"Bad Request details"
    http_err = urllib.error.HTTPError(
        "http://dummy", 400, "Bad Request", mock_headers, fp
    )
    mock_urlopen.side_effect = http_err

    with pytest.raises(ValueError) as excinfo:
        _send_request("http://dummy", {}, {}, provider="anthropic")

    assert "Anthropic HTTP Error 400: Bad Request details" in str(excinfo.value)


@patch("pr_review._send_request")
def test_make_anthropic_request_success(mock_send_request: MagicMock) -> None:
    from pr_review import make_anthropic_request

    mock_send_request.return_value = '{"summary": "Review complete"}'

    result = make_anthropic_request("fake_key", "fake_diff")

    assert result == '{"summary": "Review complete"}'
    mock_send_request.assert_called_once()
    args, kwargs = mock_send_request.call_args
    payload = args[1]
    headers = args[2]
    # Anthropic uses a top-level system prompt and no OpenRouter-specific params.
    assert kwargs.get("provider") == "anthropic"
    assert payload["model"] == "claude-haiku-4-5"
    assert "system" in payload
    assert "response_format" not in payload
    assert "temperature" not in payload
    assert "reasoning" not in payload
    assert payload["messages"][0]["role"] == "user"
    assert headers["x-api-key"] == "fake_key"
    assert headers["anthropic-version"] == "2023-06-01"


@patch("pr_review._send_request")
def test_make_anthropic_request_comments_only(mock_send_request: MagicMock) -> None:
    from pr_review import make_anthropic_request

    mock_send_request.return_value = '{"comments": []}'

    result = make_anthropic_request("fake_key", "fake_diff", post_summary=False)

    assert result == '{"comments": []}'
    payload = mock_send_request.call_args[0][1]
    system_msg = payload["system"]
    # The comments-only instruction file omits the "thinking" pass workflow.
    assert "comments" in system_msg
    assert "thinking" not in system_msg


@patch("pr_review._send_request")
def test_make_anthropic_request_oauth_headers(mock_send_request: MagicMock) -> None:
    """A Claude Pro/Max OAuth token authenticates via Bearer + the oauth beta header."""
    from pr_review import make_anthropic_request

    mock_send_request.return_value = '{"summary": "Review complete"}'

    make_anthropic_request("fake_oauth_token", "fake_diff", use_oauth=True)

    args, kwargs = mock_send_request.call_args
    payload = args[1]
    headers = args[2]
    assert kwargs.get("provider") == "anthropic"
    assert headers["Authorization"] == "Bearer fake_oauth_token"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-api-key" not in headers
    # OAuth-authenticated requests must open with Claude Code's own identity line.
    assert payload["system"].startswith(
        "You are Claude Code, Anthropic's official CLI for Claude."
    )


@patch("pr_review._send_request")
def test_make_anthropic_request_api_key_no_oauth_beta_header(
    mock_send_request: MagicMock,
) -> None:
    """A plain API key must not carry the OAuth beta header or identity prefix."""
    from pr_review import make_anthropic_request

    mock_send_request.return_value = '{"summary": "Review complete"}'

    make_anthropic_request("fake_key", "fake_diff", use_oauth=False)

    args, _kwargs = mock_send_request.call_args
    payload = args[1]
    headers = args[2]
    assert "anthropic-beta" not in headers
    assert "Authorization" not in headers
    assert not payload["system"].startswith("You are Claude Code")


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "fake_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_success_anthropic_provider(
    mock_submit: MagicMock,
    mock_make_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Verify main() dispatches to the Anthropic provider when PROVIDER=anthropic."""
    from pr_review import main

    mock_exists.return_value = True

    mock_file = MagicMock()
    mock_file.read.return_value = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,5 +1,5 @@
 line1
 line2
 line3
 line4
+line5
"""
    mock_open.return_value.__enter__.return_value = mock_file

    json_response = {
        "summary": "Looks good!",
        "comments": [{"path": "test.py", "line": 5, "body": "Nice change"}],
    }
    mock_make_req.return_value = json.dumps(json_response)

    main()

    mock_make_req.assert_called_once()
    mock_submit.assert_called_once()
    args = mock_submit.call_args[0]
    assert args[4][0]["side"] == "RIGHT"


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "anthropic",
        "CLAUDE_CODE_OAUTH_TOKEN": "fake_oauth_token",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_success_anthropic_provider_oauth_only(
    mock_submit: MagicMock,
    mock_make_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Verify main() falls back to CLAUDE_CODE_OAUTH_TOKEN when no API key is set."""
    from pr_review import main

    mock_exists.return_value = True

    mock_file = MagicMock()
    mock_file.read.return_value = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,5 +1,5 @@
 line1
 line2
 line3
 line4
+line5
"""
    mock_open.return_value.__enter__.return_value = mock_file

    mock_make_req.return_value = json.dumps({"summary": "Looks good!", "comments": []})

    main()

    mock_make_req.assert_called_once()
    kwargs = mock_make_req.call_args.kwargs
    assert kwargs["api_key"] == "fake_oauth_token"
    assert kwargs["use_oauth"] is True
    mock_submit.assert_called_once()


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "fake_key",
        "CLAUDE_CODE_OAUTH_TOKEN": "fake_oauth_token",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_anthropic_oauth_takes_precedence_over_api_key(
    mock_submit: MagicMock,
    mock_make_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """When both credentials are set, CLAUDE_CODE_OAUTH_TOKEN wins over the billed API key."""
    from pr_review import main

    mock_exists.return_value = True

    mock_file = MagicMock()
    mock_file.read.return_value = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,5 +1,5 @@
 line1
 line2
 line3
 line4
+line5
"""
    mock_open.return_value.__enter__.return_value = mock_file

    mock_make_req.return_value = json.dumps({"summary": "Looks good!", "comments": []})

    main()

    kwargs = mock_make_req.call_args.kwargs
    assert kwargs["api_key"] == "fake_oauth_token"
    assert kwargs["use_oauth"] is True


def test_main_invalid_provider() -> None:
    """Verify main() exits with 1 when PROVIDER is not a supported value."""
    from pr_review import main

    with patch.dict(
        "os.environ",
        {
            "PROVIDER": "bogus",
            "PR_NUMBER": "12",
            "REPO": "owner/repo",
            "GH_TOKEN": "fake_token",
        },
        clear=True,
    ):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1


def test_main_missing_anthropic_key() -> None:
    """Verify main() exits with 1 when PROVIDER=anthropic but neither credential is set."""
    from pr_review import main

    with patch.dict(
        "os.environ",
        {
            "PROVIDER": "anthropic",
            "PR_NUMBER": "12",
            "REPO": "owner/repo",
            "GH_TOKEN": "fake_token",
        },
        clear=True,
    ):
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1


def test_is_token_or_server_error() -> None:
    """Verify is_token_or_server_error accurately identifies token and server errors."""
    import urllib.error
    from pr_review import is_token_or_server_error

    # HTTP status codes that represent token or server errors
    assert is_token_or_server_error(
        ValueError("Anthropic HTTP Error 401: Unauthorized")
    )
    assert is_token_or_server_error(ValueError("Anthropic HTTP Error 403: Forbidden"))
    assert is_token_or_server_error(
        ValueError("Anthropic HTTP Error 429: Too Many Requests")
    )
    assert is_token_or_server_error(
        ValueError("Anthropic HTTP Error 500: Internal Server Error")
    )
    assert is_token_or_server_error(ValueError("Anthropic HTTP Error 502: Bad Gateway"))
    assert is_token_or_server_error(
        ValueError("Anthropic HTTP Error 503: Service Unavailable")
    )
    assert is_token_or_server_error(
        ValueError("Anthropic HTTP Error 504: Gateway Timeout")
    )
    assert is_token_or_server_error(ValueError("Anthropic HTTP Error 529: Overloaded"))
    assert is_token_or_server_error(
        ValueError("OpenRouter HTTP Error 402: Payment Required")
    )

    # Direct urllib.error.HTTPError
    mock_hdrs = MagicMock()
    mock_fp = MagicMock()
    assert is_token_or_server_error(
        urllib.error.HTTPError(
            url="http://test", code=401, msg="Unauthorized", hdrs=mock_hdrs, fp=mock_fp
        )
    )
    assert is_token_or_server_error(
        urllib.error.HTTPError(
            url="http://test",
            code=402,
            msg="Payment Required",
            hdrs=mock_hdrs,
            fp=mock_fp,
        )
    )
    assert is_token_or_server_error(
        urllib.error.HTTPError(
            url="http://test", code=529, msg="Overloaded", hdrs=mock_hdrs, fp=mock_fp
        )
    )

    # Chained exception __cause__ unwrapping (including multi-level chains)
    cause_http = urllib.error.HTTPError(
        url="http://test", code=401, msg="Unauthorized", hdrs=mock_hdrs, fp=mock_fp
    )
    chained_http = ValueError("Wrapped API error")
    chained_http.__cause__ = cause_http
    deep_chained_http = RuntimeError("Outer error")
    deep_chained_http.__cause__ = chained_http
    assert is_token_or_server_error(chained_http)
    assert is_token_or_server_error(deep_chained_http)

    cause_url = urllib.error.URLError("Connection reset")
    chained_url = ValueError("Wrapped network error")
    chained_url.__cause__ = cause_url
    assert is_token_or_server_error(chained_url)

    cause_timeout = TimeoutError("Timed out")
    chained_timeout = ValueError("Wrapped timeout error")
    chained_timeout.__cause__ = cause_timeout
    assert is_token_or_server_error(chained_timeout)

    # URLError and TimeoutError
    assert is_token_or_server_error(urllib.error.URLError("Connection refused"))
    assert is_token_or_server_error(TimeoutError("Request timed out"))

    # Anthropic / OpenRouter error messages and types
    assert is_token_or_server_error(
        ValueError("Anthropic Error: authentication_error: invalid x-api-key")
    )
    assert is_token_or_server_error(
        ValueError("Anthropic Error: permission_error: token revoked")
    )
    assert is_token_or_server_error(
        ValueError("Anthropic Error: rate_limit_error: quota exceeded")
    )
    assert is_token_or_server_error(
        ValueError("Anthropic Error: overloaded_error: service overloaded")
    )
    assert is_token_or_server_error(
        ValueError("Anthropic Error: api_error: internal server error")
    )
    assert is_token_or_server_error(ValueError("Credit balance is too low"))
    assert is_token_or_server_error(
        ValueError("Your expired token cannot access this resource")
    )
    # Anthropic credit balance error returned under HTTP 400 with invalid_request_error
    assert is_token_or_server_error(
        ValueError(
            'Anthropic HTTP Error 400: {"type":"error","error":{"type":"invalid_request_error",'
            '"message":"Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing."}}'
        )
    )

    # Negative cases: client / syntax / JSON errors MUST NOT be classified as token or server errors
    assert not is_token_or_server_error(
        ValueError("Anthropic HTTP Error 400: Bad Request")
    )
    assert not is_token_or_server_error(
        ValueError("Anthropic HTTP Error 404: Not Found")
    )
    assert not is_token_or_server_error(
        ValueError("Anthropic HTTP Error 413: Request Entity Too Large")
    )
    assert not is_token_or_server_error(
        ValueError("Anthropic HTTP Error 422: Unprocessable Entity")
    )
    assert not is_token_or_server_error(
        urllib.error.HTTPError(
            url="http://test", code=400, msg="Bad Request", hdrs=mock_hdrs, fp=mock_fp
        )
    )
    assert not is_token_or_server_error(
        ValueError(
            "Anthropic Error: invalid_request_error: temperature parameter is not supported"
        )
    )
    assert not is_token_or_server_error(
        ValueError("Anthropic refused to process the request (stop_reason: refusal).")
    )
    assert not is_token_or_server_error(
        ValueError("No valid JSON object found in LLM response")
    )
    assert not is_token_or_server_error(ValueError("Anthropic returned empty content."))


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "auto",
        "CLAUDE_CODE_OAUTH_TOKEN": "expired_oauth_token",
        "OPENROUTER_API_KEY": "valid_openrouter_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_fallback_from_expired_claude_token_to_openrouter(
    mock_submit: MagicMock,
    mock_anthropic_req: MagicMock,
    mock_openrouter_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Verify main() falls back from an expired Claude OAuth token (401) to OpenRouter."""
    from pr_review import main

    mock_exists.return_value = True
    mock_file = MagicMock()
    mock_file.read.return_value = (
        "diff --git a/test.py b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    mock_open.return_value.__enter__.return_value = mock_file

    mock_anthropic_req.side_effect = ValueError(
        'Anthropic HTTP Error 401: {"type":"error","error":{"type":"authentication_error","message":"invalid token"}}'
    )
    mock_openrouter_req.return_value = json.dumps(
        {"summary": "Fallback review complete", "comments": []}
    )

    main()

    # Claude was attempted first using oauth
    mock_anthropic_req.assert_called_once()
    assert mock_anthropic_req.call_args.kwargs["use_oauth"] is True
    # Fallback to OpenRouter was called
    mock_openrouter_req.assert_called_once()
    assert mock_openrouter_req.call_args.kwargs["api_key"] == "valid_openrouter_key"
    mock_submit.assert_called_once()


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "auto",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth_token",
        "OPENROUTER_API_KEY": "openrouter_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_fallback_from_claude_529_overloaded_to_openrouter(
    mock_submit: MagicMock,
    mock_anthropic_req: MagicMock,
    mock_openrouter_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Verify main() falls back from Claude on 529 Overloaded error to OpenRouter."""
    from pr_review import main

    mock_exists.return_value = True
    mock_file = MagicMock()
    mock_file.read.return_value = (
        "diff --git a/test.py b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    mock_open.return_value.__enter__.return_value = mock_file

    mock_anthropic_req.side_effect = ValueError("Anthropic HTTP Error 529: Overloaded")
    mock_openrouter_req.return_value = json.dumps(
        {"summary": "Review after 529", "comments": []}
    )

    main()

    mock_anthropic_req.assert_called_once()
    mock_openrouter_req.assert_called_once()
    mock_submit.assert_called_once()


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "auto",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth_token",
        "OPENROUTER_API_KEY": "openrouter_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("time.sleep")
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_no_fallback_on_400_bad_request(
    mock_submit: MagicMock,
    mock_anthropic_req: MagicMock,
    mock_openrouter_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
    mock_sleep: MagicMock,
) -> None:
    """Verify main() does NOT fall back to OpenRouter on client 400 Bad Request errors."""
    from pr_review import main

    mock_exists.return_value = True
    mock_file = MagicMock()
    mock_file.read.return_value = (
        "diff --git a/test.py b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    mock_open.return_value.__enter__.return_value = mock_file

    mock_anthropic_req.side_effect = ValueError(
        "Anthropic HTTP Error 400: Bad Request - invalid parameter"
    )

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1

    # Retried 3 times on deterministic client error without falling back
    assert mock_anthropic_req.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_has_calls([call(1.0), call(2.0)])
    mock_openrouter_req.assert_not_called()
    mock_submit.assert_not_called()


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "auto",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth_token",
        "ANTHROPIC_API_KEY": "billed_key",
        "OPENROUTER_API_KEY": "openrouter_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_fallback_cascade_oauth_to_api_key_to_openrouter(
    mock_submit: MagicMock,
    mock_anthropic_req: MagicMock,
    mock_openrouter_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Verify main() cascades from OAuth token error to API key error to OpenRouter."""
    from pr_review import main

    mock_exists.return_value = True
    mock_file = MagicMock()
    mock_file.read.return_value = (
        "diff --git a/test.py b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    mock_open.return_value.__enter__.return_value = mock_file

    mock_anthropic_req.side_effect = [
        ValueError("Anthropic HTTP Error 401: Unauthorized"),  # OAuth token failure
        ValueError("Anthropic HTTP Error 429: Rate limit exceeded"),  # API key failure
    ]
    mock_openrouter_req.return_value = json.dumps(
        {"summary": "OpenRouter cascade review", "comments": []}
    )

    main()

    assert mock_anthropic_req.call_count == 2
    # First attempt used OAuth token
    assert mock_anthropic_req.call_args_list[0].kwargs["use_oauth"] is True
    assert mock_anthropic_req.call_args_list[0].kwargs["api_key"] == "oauth_token"
    # Second attempt cascaded to billed API key
    assert mock_anthropic_req.call_args_list[1].kwargs["use_oauth"] is False
    assert mock_anthropic_req.call_args_list[1].kwargs["api_key"] == "billed_key"

    # Third attempt fell back to OpenRouter
    mock_openrouter_req.assert_called_once()
    assert mock_openrouter_req.call_args.kwargs["api_key"] == "openrouter_key"
    mock_submit.assert_called_once()
    assert mock_submit.call_args[0][3] == "OpenRouter cascade review"


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "billed_key",
        "OPENROUTER_API_KEY": "openrouter_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("time.sleep")
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_explicit_anthropic_does_not_fallback_to_openrouter(
    mock_submit: MagicMock,
    mock_anthropic_req: MagicMock,
    mock_openrouter_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
    mock_sleep: MagicMock,
) -> None:
    """Verify PROVIDER=anthropic strictly stays on Anthropic and never falls back to OpenRouter."""
    from pr_review import main

    mock_exists.return_value = True
    mock_file = MagicMock()
    mock_file.read.return_value = (
        "diff --git a/test.py b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    mock_open.return_value.__enter__.return_value = mock_file

    mock_anthropic_req.side_effect = ValueError(
        "Anthropic HTTP Error 401: Unauthorized"
    )

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1

    mock_anthropic_req.assert_called()
    mock_openrouter_req.assert_not_called()
    mock_submit.assert_not_called()


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "auto",
        "OPENROUTER_API_KEY": "openrouter_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("time.sleep")
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.submit_github_review")
def test_main_token_error_no_fallback_candidate_exits(
    mock_submit: MagicMock,
    mock_openrouter_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
    mock_sleep: MagicMock,
) -> None:
    """Verify main() retries and exits when a token error occurs and no further fallback is available."""
    from pr_review import main

    mock_exists.return_value = True
    mock_file = MagicMock()
    mock_file.read.return_value = (
        "diff --git a/test.py b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    mock_open.return_value.__enter__.return_value = mock_file

    mock_openrouter_req.side_effect = ValueError(
        "OpenRouter HTTP Error 401: Unauthorized"
    )

    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1

    assert mock_openrouter_req.call_count == 3
    assert mock_sleep.call_count == 2
    mock_submit.assert_not_called()


@patch.dict(
    "os.environ",
    {
        "PROVIDER": "openrouter",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth_token",
        "OPENROUTER_API_KEY": "openrouter_key",
        "PR_NUMBER": "12",
        "REPO": "owner/repo",
        "GH_TOKEN": "fake_github_token",
        "POST_SUMMARY": "true",
    },
    clear=True,
)
@patch("os.path.exists")
@patch("builtins.open")
@patch("pr_review.make_openrouter_request")
@patch("pr_review.make_anthropic_request")
@patch("pr_review.submit_github_review")
def test_main_explicit_openrouter_bypasses_claude(
    mock_submit: MagicMock,
    mock_anthropic_req: MagicMock,
    mock_openrouter_req: MagicMock,
    mock_open: MagicMock,
    mock_exists: MagicMock,
) -> None:
    """Verify PROVIDER=openrouter directly uses OpenRouter and ignores Claude tokens."""
    from pr_review import main

    mock_exists.return_value = True
    mock_file = MagicMock()
    mock_file.read.return_value = (
        "diff --git a/test.py b/test.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    mock_open.return_value.__enter__.return_value = mock_file

    mock_openrouter_req.return_value = json.dumps(
        {"summary": "Direct OpenRouter review", "comments": []}
    )

    main()

    mock_anthropic_req.assert_not_called()
    mock_openrouter_req.assert_called_once()
    mock_submit.assert_called_once()


# --- Sanitizing model-written text (indirect prompt injection via the PR diff) ---
#
# The sanitizer has to make model output inert on GitHub, so most of these tests render
# the sanitized text with cmark-gfm (the parser GitHub's renderer is built on) and assert
# that nothing active comes out. That checks the property that matters, instead of
# pinning an escaping format that a safe future change might legitimately alter. Exact
# outputs are only pinned where legitimate review text must survive unchanged.

_CMARK = shutil.which("cmark-gfm")
# Block and inline tags that cannot load anything or navigate anywhere.
_INERT_TAGS = {
    "p", "em", "strong", "del", "ul", "ol", "li", "blockquote", "h1", "h2", "h3",
    "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "hr", "br",
    "input", "code", "pre",
}  # fmt: skip


def _render(markdown: str) -> str:
    assert _CMARK
    return subprocess.run(
        [
            _CMARK,
            "-e",
            "table",
            "-e",
            "autolink",
            "-e",
            "strikethrough",
            "-e",
            "tasklist",
        ]
        + ["--unsafe"],  # let raw HTML through, so it can be detected
        input=markdown,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout


def _live_elements(html: str) -> list[str]:
    """List what GitHub would render as active outside code: links, images, HTML,
    mentions and cross-repository references."""
    # GitHub gives some fence languages special behaviour (suggestion blocks, diagrams),
    # so a language that is not on our allowlist counts as active even though it is code.
    found = [
        f"fence language: {language}"
        for language in re.findall(r'<code class="language-([^"]*)"', html)
        if language not in _FENCE_LANGUAGES
    ]
    # Code ends one text node and starts the next, so leave a gap instead of joining the
    # text on either side into something GitHub would never see as one string.
    html = re.sub(r"<pre\b.*?</pre>", " ", html, flags=re.DOTALL)
    html = re.sub(r"<code\b.*?</code>", " ", html, flags=re.DOTALL)
    if re.search(r"<\s*a[\s/>]", html, flags=re.IGNORECASE):
        found.append("link")
    if re.search(r"<\s*img[\s/>]", html, flags=re.IGNORECASE):
        found.append("image")
    for tag in re.findall(r"<\s*([a-zA-Z][a-zA-Z0-9]*)", html):
        if tag.lower() not in _INERT_TAGS | {"a", "img"}:
            found.append(f"html:{tag}")
            break
    text = re.sub(r"<[^>]+>", " ", html)
    if re.search(r"(?<![A-Za-z0-9_])@[A-Za-z0-9]", text):
        found.append("mention")
    if re.search(r"(?<![\w/.-])[A-Za-z0-9-]+/[A-Za-z0-9_.-]+#\d+", text):
        found.append("cross-repo reference")
    return found


@pytest.fixture
def require_cmark() -> None:
    """Fail in CI, where a missing oracle would silently turn the checks off; skip locally."""
    if _CMARK is None:
        if os.environ.get("CI"):
            pytest.fail(
                "cmark-gfm is required for the sanitizer tests in CI (shell.nix)"
            )
        pytest.skip("cmark-gfm not found; it is provided by the nix shell")


@pytest.fixture
def live_elements(require_cmark: None) -> Callable[[str], list[str]]:
    return lambda markdown: _live_elements(_render(markdown))


# Each entry is a way to get something active onto the thread. Most were found by
# reviewing earlier versions of the sanitizer, which let them through.
_HOSTILE_MARKDOWN = {
    "mention": "cc @octocat",
    "team-mention": "ping @my-org/reviewers",
    "mention-in-parens": "(@octocat)",
    "mention-in-emphasis": "_@octocat_ and __@octocat__ and ~~@octocat~~",
    "mention-entity": "&#x40;octocat and &#64;octocat",
    "mention-after-backslash": "\\@octocat",
    "mention-after-stray-backtick": "` @octocat",
    "mention-in-multiline-span": "`foo\n@octocat bar`",
    "mention-with-mismatched-backticks": "``@octocat`",
    "mention-after-escaped-backtick": "\\`@octocat\\`",
    "mention-in-table": "| a | b |\n| - | - |\n| `x | @octocat` |",
    # A table needs no pipe in its header or delimiter row (`:-` alone will do), and it
    # splits its cells at every pipe in the following rows, even inside a code span.
    "table-pipeless-span-with-link": "x\n:-\n`[click](//evil.example)|`",
    "table-pipeless-span-with-image": "x\n:-\n`<img src=//evil.example/p.png>|`",
    "table-pipeless-span-with-mention": "x\n:-\n`@octocat|`",
    "table-pipeless-url-with-pipe": "x\n:-\nhttps://evil.example/a|b",
    "table-pipeless-right-aligned": "x\n-:\n`[click](//evil.example)|`",
    "table-pipeless-centered": "x\n:-:\n`[click](//evil.example)|`",
    "table-pipeless-span-in-header": "`a`\n:-\n`[click](//evil.example)|`",
    "table-pipeless-backtick-beside-pipe": "x\n:-\n``<img src=//e.example/i.png>`|`b``",
    "cross-repo-reference": "see octo/repo#12",
    "cross-repo-reference-in-emphasis": "_octo/repo#1_",
    "cross-repo-references-chained": "octo/repo#1octo/repo#2 and a/b#1c/d#2",
    "image": "![x](https://evil.example/p.png)",
    "image-protocol-relative": "![x](//evil.example/p.png)",
    "image-nested-in-link": "[![a](i)](//evil.example)",
    "image-balanced-brackets": "![[x]](//evil.example/p.png)",
    "image-reference-style": "![a][r]\n\n[r]: //evil.example/p.png",
    "image-via-blockquote-definition": "> [x]: //evil.example/p.png\n\n![x] and [x]",
    "link": "[a](https://example.com)",
    "link-protocol-relative": "[click](//evil.example)",
    "link-nested": "[[x](a)](//evil.example)",
    "link-balanced-brackets": "[[x]](//evil.example)",
    "link-newline-in-text": "[click\nhere](//evil.example)",
    "link-lone-backtick-in-text": "[a`b](//evil.example)",
    "link-escaped-bracket": "[a\\]b](//evil.example)",
    "link-reference-style": "[a][ref]\n\n[ref]: //evil.example",
    "link-definition-in-list": "- [x]: //evil.example\n\n[click][x]",
    "link-javascript": "[x](javascript:alert(1))",
    "bare-url": "see https://evil.example/x for details",
    "bare-url-after-backslash": "\\https://evil.example",
    "bare-url-in-table": "| a | b |\n| - | - |\n| https://evil.example/a|b |",
    "www-url": "www.evil.example/page",
    "ftp-url": "ftp://evil.example/x",
    "mailto": "mailto:someone@evil.example",
    "email": "write to someone@evil.example",
    # GitHub's autolinker links an address whose domain starts with `.`, `-` or `_`, which
    # neither the email pattern nor the mention pattern matched, and that also holds
    # right after another token, where the email pattern cannot start.
    "email-dot-after-at": "mail a@.evil.example",
    "email-dot-after-at-no-tld-label": "a@.b",
    "email-dot-after-at-dotted-local-part": "a.b@.c.d",
    "email-dot-after-at-plus-local-part": "+a@.b.co",
    "email-dot-after-at-many-labels": "a@.b.c.d.e",
    "email-dot-after-at-then-path": "a@.b.co/path",
    "email-dot-after-at-in-table": "| a | b |\n| - | - |\n| x@.evil.example |",
    "email-dot-after-at-chained": "a@.b@c.d",
    "email-dash-after-at-after-xref": "o/r#1a@-.evil.example",
    "email-underscore-after-at-after-xref": "o/r#1a@_.evil.example",
    "email-dot-after-at-after-xref": "o/r#1a@.evil.example",
    "email-dot-after-at-after-mention": "@aaa@.evil.example",
    "email-dot-after-at-after-span": "`x`a@.evil.example",
    "angle-autolink": "<https://evil.example/x>",
    "html-image": "<img src=//evil.example/q.png>",
    "html-image-in-table": "| a | b |\n| - | - |\n| `x | <img src=//evil.example/p.png>` |",
    "html-link": '<a href="//evil.example">hi</a>',
    "html-script": "<script>alert(1)</script>",
    "html-details": "<details><summary>x</summary>y</details>",
    "html-comment": "<!-- hidden --> text",
    "fence-closed-by-indented-closer": "```\nx\n ```\n@octocat [a](//evil.example) <img src=//evil.example/q.png>",
    "fence-opened-by-indented-opener": " ```\nx\n```\n@octocat [a](//evil.example)",
    "fence-closed-by-longer-closer": "```\nx\n`````\n@octocat",
    "fence-backtick-in-info-string": "```a`b\n@octocat",
    "fence-tilde-then-indented-closer": "~~~\n@a\n  ~~~\n@octocat",
    "fence-suggestion": "```suggestion\nbackdoor()\n```",
    # An opener indented 1-3 spaces opens a fence for GitHub, so tildes need the same
    # care as backticks, including their info strings.
    "fence-indented-tilde-opener": "intro\n ~~~\n```\n~~~\n@octocat [a](//evil.example) <img src=//evil.example/q.png>",
    "fence-three-space-tilde-opener": "intro\n   ~~~\n```\n~~~\n@octocat [a](//evil.example) <img src=//evil.example/q.png>",
    "fence-indented-tilde-suggestion": "intro\n ~~~suggestion\nx\n~~~",
    "fence-indented-tilde-mermaid": "intro\n  ~~~mermaid\ngraph TD\n~~~",
    "fence-quoted-tilde-suggestion": "> ~~~suggestion\n> x\n> ~~~",
    # Spans that touch, and backtick runs of different lengths: they used to fuse and
    # re-pair, which moved text out of its code span.
    "spans-user-then-token": "``@octocat`x``@a",
    "spans-token-then-user": "@a`` `q <img src=//evil.example/x> ``",
    "spans-token-then-span-with-link": "@a``x`[c](//e.example)``",
    "spans-span-with-link-then-token": "``[c](//e.example)`y``@a",
    "spans-mixed-runs": "@a``b`c``@d",
    "spans-ascending-runs": "`a``b```c````d`````e @octocat [a](//e.example) o/r#1",
    "spans-descending-runs": "`````a````b```c``d`e @octocat [a](//e.example) o/r#1",
    "spans-runs-around-a-link": "`a``[c](//e.example)``b` @octocat",
    "spans-pipe-span-then-link-span": "`a|b` `[c](//e.example)` @octocat",
    "spans-pipe-span-then-token": "`a|b`@octocat`c|d`https://evil.example",
    "alert-block": "> [!CAUTION]\n> Verified by maintainers",
}


def _in_context(markdown: str, context: str) -> str:
    """Place a payload inside a blockquote or a list item, where rendering differs."""
    if context == "quoted":
        return "\n".join("> " + line for line in markdown.split("\n"))
    if context == "list":
        first, *rest = markdown.split("\n")
        return "\n".join(["- " + first] + ["  " + line for line in rest])
    return markdown


@pytest.mark.parametrize("context", ["plain", "quoted", "list"])
@pytest.mark.parametrize("name", sorted(_HOSTILE_MARKDOWN))
def test_sanitized_hostile_markdown_renders_nothing_active(
    name: str, context: str, live_elements: Callable[[str], list[str]]
) -> None:
    """Whatever the payload, and wherever it sits, the posted text has no live element."""
    sanitized = sanitize_llm_text(_in_context(_HOSTILE_MARKDOWN[name], context), 10000)
    assert live_elements(sanitized) == []


# Both of the sanitizer's worst bugs were interactions between pieces that each looked
# fine alone, which a hand-written list of single-idea payloads cannot find. So stitch
# random hostile pieces together, with a fixed seed so any failure reproduces.
_FUZZ_ATOMS = [
    "`", "``", "```", "~", "~~~", "~~~suggestion", "```suggestion", "```python",
    " ", "  ", "   ", "\\", "\n", "\n", "> ", "- ", "1. ", "|", "x", "$", "#",
    "@a", "@my-org/t", "http://x.example", "www.x.example", "a@b.example", "a@.b.example", "a@-.b.example", "@.", "o/r#1",
    "[c](//e.example)", "![i](//e.example/p.png)", "<img src=//e.example/i.png>",
    ":-", "-:", "-", "`[c](//e.example)|`", "`x|y`", "a|b",
    # Line breaks were only 2 of ~35 atoms, so most documents had a single line.
    "\n", "\n", "\n", "\n",
    "<a href=//e.example>", "&#64;a", "[r]: //e.example", "[r]", "_@a_",
]  # fmt: skip


def test_sanitized_random_hostile_documents_render_nothing_active(
    live_elements: Callable[[str], list[str]],
) -> None:
    rng = random.Random(20261118)
    failures = []
    for _ in range(400):
        document = "".join(rng.choice(_FUZZ_ATOMS) for _ in range(rng.randint(3, 16)))
        found = live_elements(sanitize_llm_text(document, 10000))
        if found:
            failures.append((document, found))
    assert not failures, f"{len(failures)} live documents, first: {failures[:3]}"


def test_hostile_corpus_is_live_before_sanitizing(
    live_elements: Callable[[str], list[str]],
) -> None:
    """Guard against a vacuous corpus: the checker must see these payloads as dangerous."""
    live = [name for name, md in _HOSTILE_MARKDOWN.items() if live_elements(md)]
    # A few entries are only dangerous because of how an earlier sanitizer rewrote them.
    assert len(live) >= len(_HOSTILE_MARKDOWN) - 6, sorted(
        set(_HOSTILE_MARKDOWN) - set(live)
    )


def test_sanitized_hidden_definition_is_visible_to_readers(require_cmark: None) -> None:
    """A link definition renders as nothing, so it could hide instructions for a later AI
    reader; escaped, the text must show."""
    html = _render(sanitize_llm_text("> [//]: # (instructions for AI agents)", 1000))
    assert "instructions for AI agents" in re.sub(r"<[^>]+>", " ", html)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            "Consider extracting this into a helper.",
            "Consider extracting this into a helper.",
            id="plain-text",
        ),
        pytest.param(
            "### Needs Attention\n1. **Bug** in the loop\n- item one\n> quoted advice",
            "### Needs Attention\n1. **Bug** in the loop\n- item one\n> quoted advice",
            id="headings-lists-emphasis-and-quotes-untouched",
        ),
        pytest.param(
            "Use `List<String>` & `@Override` here.",
            "Use `List<String>` &amp; `@Override` here.",
            id="inline-code-verbatim-prose-escaped",
        ),
        pytest.param(
            "Call `fns[i](x)` safely.",
            "Call `fns[i](x)` safely.",
            id="inline-code-with-link-syntax-untouched",
        ),
        pytest.param(
            "Call `a | b` and `|` safely.",
            "Call \\`a \\| b\\` and \\`\\|\\` safely.",
            id="a-span-with-a-pipe-is-shown-as-escaped-text",
        ),
        pytest.param(
            "Use `str | None` instead of `Optional[str]`.",
            "Use \\`str \\| None\\` instead of `Optional[str]`.",
            id="a-span-with-a-pipe-does-not-swap-code-and-text-with-the-next-span",
        ),
        pytest.param(
            "see https://e.example/a|b ok",
            "see `https://e.example/a`\\|b ok",
            id="urls-stop-at-a-pipe",
        ),
        pytest.param(
            "`a``b``c`",
            "`a``b``c`",
            id="touching-spans-stay-as-written",
        ),
        pytest.param(
            "Run ``a`b`` now.",
            "Run \\``a`b\\`\\` now.",
            id="double-backtick-code-degrades-to-escaped-backticks",
        ),
        pytest.param(
            "``@octocat`x``@a",
            "\\``@octocat`x\\`\\``@a`",
            id="backticks-that-do-not-pair-are-escaped",
        ),
        pytest.param(
            "intro\n ~~~suggestion\nx",
            "intro\n \\~\\~\\~suggestion\nx",
            id="indented-tilde-line-cannot-open-a-fence",
        ),
        pytest.param(
            "Before:\n```python\nif a < b and c & d:\n    x = '@octocat'  # [a](b)\n```\nAfter.",
            "Before:\n```python\nif a < b and c & d:\n    x = '@octocat'  # [a](b)\n```\nAfter.",
            id="fenced-code-verbatim",
        ),
        pytest.param(
            "```Python title=x\ncode\n```",
            "```python\ncode\n```",
            id="fence-keeps-allowlisted-language-only",
        ),
        pytest.param(
            "```suggestion\nx = 1\n```", "```text\nx = 1\n```", id="suggestion-fence"
        ),
        pytest.param(
            "~~~mermaid\ng\n~~~", "~~~text\ng\n~~~", id="special-renderer-fence"
        ),
        pytest.param(
            "```\ncode\n  ```\n@octocat",
            "```\ncode\n```\n`@octocat`",
            id="indented-closer-ends-the-block",
        ),
        pytest.param(
            "intro\n  ```\n@octocat\n```\nafter",
            "intro\n  \\`\\`\\`\n`@octocat`\n```\nafter\n```",
            id="indented-opener-is-prose",
        ),
        pytest.param(
            "```\n@octocat",
            "```\n@octocat\n```",
            id="unterminated-fence-is-closed-so-later-text-is-not-swallowed",
        ),
        pytest.param(
            "~~~~\nx", "~~~~\nx\n~~~~", id="unterminated-tilde-fence-is-closed"
        ),
        pytest.param(
            "cc @octocat and @my-org/team",
            "cc `@octocat` and `@my-org/team`",
            id="mentions-become-code",
        ),
        pytest.param(
            "see octo/repo#12 and #12 and a@example.com",
            "see `octo/repo#12` and #12 and `a@example.com`",
            id="cross-repo-ref-and-email-become-code-local-ref-kept",
        ),
        pytest.param(
            "mail a@.example.com or a@.b.co/path",
            "mail a`@.example.com` or a`@.b.co`/path",
            id="at-sign-before-a-dot-domain-becomes-code",
        ),
        pytest.param(
            "o/r#1a@-.b.co",
            "`o/r#1`a`@-.b.co`",
            id="at-sign-after-a-token-is-wrapped-though-no-email-matches",
        ),
        pytest.param(
            "see https://example.com/x, or www.example.org",
            "see `https://example.com/x,` or `www.example.org`",
            id="urls-become-code",
        ),
        pytest.param(
            "[docs](https://example.com) ![i](//e.example/p.png)",
            "\\[docs\\](`https://example.com)` !\\[i\\](//e.example/p.png)",
            id="link-and-image-syntax-cannot-form",
        ),
        pytest.param(
            "a & b <c> | d [e] \\ f",
            "a &amp; b &lt;c> \\| d \\[e\\] \\\\ f",
            id="active-characters-escaped",
        ),
        pytest.param(
            "` @octocat", "\\` `@octocat`", id="stray-backtick-cannot-pair-with-ours"
        ),
        pytest.param(
            "\\@octocat", "\\\\`@octocat`", id="backslash-cannot-escape-our-span"
        ),
        pytest.param("R&D &#64;x", "R&amp;D &amp;#64;x", id="entities-not-decoded"),
    ],
)
def test_sanitize_exact_output(raw: str, expected: str) -> None:
    """Legitimate review text survives as written, and each defence has its pinned form."""
    assert sanitize_llm_text(raw, 10000) == expected


def test_sanitize_strips_invisible_characters() -> None:
    """Hidden characters let text stay unseen by reviewers but readable by another AI."""
    tag_character = chr(0xE0041)
    variation_selector = chr(0xE0100)
    raw = (
        f"a\u200bb\u202ec{tag_character}d{variation_selector}\u034f\u2800\u3164\u2028"
        "\r\ne\tf"
    )
    assert sanitize_llm_text(raw, 1000) == "abcd\ne\tf"


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("a\ud800b", "ab", id="lone-high-surrogate"),
        pytest.param("a\udc00b", "ab", id="lone-low-surrogate"),
        pytest.param("\ude00\ud83d", "", id="surrogates-in-the-wrong-order"),
        pytest.param("`a\ud800b`", "`ab`", id="surrogate-inside-a-code-span"),
        pytest.param("```\na\ud800b\n```", "```\nab\n```", id="surrogate-in-a-fence"),
        pytest.param("ok \U0001f600", "ok \U0001f600", id="a-real-emoji-survives"),
    ],
)
def test_sanitize_strips_lone_surrogates_but_keeps_real_characters(
    raw: str, expected: str
) -> None:
    """A lone surrogate cannot be encoded as UTF-8, so it must not reach the output."""
    result = sanitize_llm_text(raw, 1000)
    assert result == expected
    result.encode("utf-8")


def test_sanitize_returns_empty_for_text_with_nothing_visible() -> None:
    assert sanitize_llm_text("\u200b\u200d\ufeff \n", 1000) == ""


def test_sanitize_truncates_at_the_limit() -> None:
    assert sanitize_llm_text("a" * 100, 100) == "a" * 100
    assert sanitize_llm_text("a" * 101, 100) == "a" * 100 + "\n\n_(truncated)_"


def test_invisible_characters_do_not_use_up_the_cap() -> None:
    """The cap applies to the text people can see, so a flood of zero-width characters
    ahead of it cannot push it out of the cap and get the comment withheld."""
    flood = "\u200b" * 5000
    assert sanitize_llm_text(flood + "visible text", 100) == "visible text"
    assert sanitize_llm_text("visible text" + flood, 100) == "visible text"


def test_sanitize_closes_a_fence_cut_by_truncation() -> None:
    """The marker must not end up inside the code block the cut left open."""
    result = sanitize_llm_text("```\n" + "x" * 100, 20)
    assert result == "```\n" + "x" * 16 + "\n```\n\n_(truncated)_"


# Inputs built to make the output as long as possible. Output beyond GitHub's 65,536
# characters makes it reject the whole review, so none of these may exceed 5x the cap.
_GROWTH_SHAPES: dict[str, Callable[[int], str]] = {
    "ampersands": lambda n: "&" * n,
    "less-thans": lambda n: "<" * n,
    "backslashes": lambda n: "\\" * n,
    "backticks": lambda n: "`" * n,
    "pipes": lambda n: "|" * n,
    "brackets": lambda n: "[]" * (n // 2),
    "short-mentions": lambda n: "@a" * (n // 2),
    "short-urls": lambda n: "www.a " * (n // 6),
    "url-with-many-pipes": lambda n: "www.a" + "|a" * ((n - 5) // 2),
    "spans-with-pipes": lambda n: "`|`" * (n // 3),
    "touching-spans": lambda n: "`a`" * (n // 3),
    "double-backtick-pairs": lambda n: "``a``" * (n // 5),
    "ascending-backtick-runs": lambda n: " ".join("`" * k + "a" for k in range(1, 85)),
}


@pytest.mark.parametrize("cap", [MAX_COMMENT_CHARS, MAX_SUMMARY_CHARS])
@pytest.mark.parametrize("shape", sorted(_GROWTH_SHAPES))
def test_sanitized_length_stays_within_five_times_the_cap(shape: str, cap: int) -> None:
    """Both the docs and the size budget rely on this: five times the cap at most. A
    ValueError here means the output went over the bound `sanitize_llm_text` enforces."""
    text = _GROWTH_SHAPES[shape](cap)
    # A shape longer than the cap would be truncated first, which can cut off the very
    # structure it is meant to exercise (a closing delimiter) and test nothing.
    assert len(text) <= cap
    result = sanitize_llm_text(text, cap)
    assert len(result) <= _MAX_GROWTH_FACTOR * cap + _OUTPUT_SLACK


def test_the_largest_possible_review_body_fits_githubs_limit() -> None:
    """GitHub rejects the whole review above 65,536 characters. The summary is the biggest
    part, and the notices and warnings added after it are a few hundred characters."""
    notices_and_warnings = 1000
    biggest_summary = _MAX_GROWTH_FACTOR * MAX_SUMMARY_CHARS + _OUTPUT_SLACK
    assert biggest_summary + notices_and_warnings < 65536


def test_sanitize_raises_instead_of_returning_an_oversized_result() -> None:
    with patch("pr_review._neutralize_prose_line", return_value="x" * 100000):
        with pytest.raises(SanitizeError, match="over"):
            sanitize_llm_text("hello", 100)


_ADVERSARIAL_SHAPES: dict[str, Callable[[int], str]] = {
    "empty-links": lambda n: "[](" * (n // 3),
    "open-brackets": lambda n: "[" * n,
    "blank-lines": lambda n: "\n" * n,
    "unclosed-link": lambda n: "[a](" + "b" * (n - 4),
    "backtick-pairs": lambda n: "`a" * (n // 2),
    "backticks-in-link-text": lambda n: "[`a" * (n // 3),
    "growing-backtick-runs": lambda n: " ".join("`" * k + "a" for k in range(1, 140)),
    "email-like": lambda n: "a." * (n // 2),
    "at-signs": lambda n: "@a" * (n // 2),
    "reference-like": lambda n: "a/" * (n // 2),
    "chained-references": lambda n: "a/b#1" * (n // 5),
    "underscores": lambda n: "_" * n,
    "mixed-fences": lambda n: "```\n~~~\n" * (n // 8),
    "ampersands": lambda n: "&" * n,
    "long-url": lambda n: "https://" + "a" * (n - 8),
}


@pytest.mark.parametrize("shape", sorted(_ADVERSARIAL_SHAPES))
def test_sanitize_is_fast_on_adversarial_input(shape: str) -> None:
    """The sanitizer reads attacker-influenced text and Python's `re` has no timeout, so a
    pattern with super-linear worst-case time would let one hostile diff hang the Action.
    The bound is generous (these run in milliseconds when everything is linear) so that a
    loaded CI runner cannot fail it, while a super-linear blow-up, which takes minutes,
    still does."""
    text = _ADVERSARIAL_SHAPES[shape](MAX_SUMMARY_CHARS)
    started = time.perf_counter()
    sanitize_llm_text(text, MAX_SUMMARY_CHARS)
    assert time.perf_counter() - started < 5.0
