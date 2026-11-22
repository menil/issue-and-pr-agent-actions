#!/usr/bin/env python3
"""Automated Pull Request Code Review Script.

This script parses a unified PR diff, fetches inline review comments and a
high-level summary from the OpenRouter API, filters comments to target
only changed lines, and submits the review to GitHub.
"""

import json
import os
import re
import sys
from typing import Any
import urllib.error
import urllib.request

from llm import (
    ProviderConfig,
    anthropic_chat,
    build_provider_candidates,
    check_credentials,
    openrouter_chat,
    request_with_fallback,
)
from sanitize import (
    MAX_COMMENT_CHARS,
    MAX_SUMMARY_CHARS,
    log_block,
    log_text,
    sanitize_or_drop,
)

# Default patterns of files to exclude from code review
DEFAULT_EXCLUDE_PATTERNS = [
    # Lock files
    r"package-lock\.json$",
    r"yarn\.lock$",
    r"pnpm-lock\.yaml$",
    r"Cargo\.lock$",
    r"poetry\.lock$",
    r"mix\.lock$",
    r"go\.sum$",
    r"composer\.lock$",
    r"pdm\.lock$",
    r"Pipfile\.lock$",
    r"uv\.lock$",
    r"bun\.lockb$",
    r"deno\.lock$",
    # Jest snapshots
    r"\.snap$",
    # Minified files
    r"\.min\.js$",
    r"\.min\.css$",
    # Build maps
    r"\.map$",
    # Images/vectors/binaries
    r"\.svg$",
    r"\.png$",
    r"\.jpg$",
    r"\.jpeg$",
    r"\.gif$",
    r"\.ico$",
]

# Pre-compiled regular expressions for efficiency
JSON_CLEAN_RE = re.compile(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])')
CLEAN_FIELDS_RE = re.compile(
    r'("(?:thinking|summary|path|body)"\s*:\s*")(.*?)("\s*(?=,\s*"[^"]+"\s*:|,?\s*\}))',
    flags=re.DOTALL | re.IGNORECASE,
)

MD_HEADER_RE = re.compile(r"^#+\s*")
MD_LIST_RE = re.compile(r"^[-*+]\s+")
MD_NUM_LIST_RE = re.compile(r"^\d+\.\s+")
MD_BOLD_START_RE = re.compile(r"^\*+\s*")
MD_BOLD_END_RE = re.compile(r"\*+\s*$")
MD_BACKTICK_START_RE = re.compile(r"^`\s*")
MD_BACKTICK_END_RE = re.compile(r"`\s*$")


def get_exclude_regexes(
    custom_patterns_str: str | None = None,
) -> list[re.Pattern[str]]:
    """Compile and return the complete list of exclusion regex patterns.

    Allows matching both default and custom-supplied exclusions.
    """
    patterns = list(DEFAULT_EXCLUDE_PATTERNS)
    if custom_patterns_str:
        # Split custom exclusions by comma or newline
        custom_list = [
            p.strip() for p in re.split(r"[,\n]", custom_patterns_str) if p.strip()
        ]
        patterns.extend(custom_list)
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def get_modified_lines(diff_text: str) -> dict[str, set[int]]:
    """Parse a unified diff and return a map of files to their modified new line numbers."""
    modified: dict[str, set[int]] = {}
    current_file: str | None = None
    new_line_num: int = 0
    in_header: bool = True

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            current_file = None
        elif line.startswith("@@"):
            in_header = False
            try:
                # Format: @@ -old_start,old_len +new_start,new_len @@ ...
                parts = line.split()
                if len(parts) >= 3:
                    new_info = parts[2]  # e.g., "+new_start,new_len" or "+new_start"
                    if new_info.startswith("+"):
                        new_start = int(new_info[1:].split(",")[0])
                        new_line_num = new_start - 1
            except (ValueError, IndexError):
                # If parsing fails, ignore this hunk's start and skip line counting
                current_file = None
        elif in_header and line.startswith("+++ "):
            path_part = line[4:]
            if path_part.startswith('"') and path_part.endswith('"'):
                path_part = path_part[1:-1]
            if path_part.startswith("b/"):
                current_file = path_part[2:]
                modified[current_file] = set()
            elif path_part == "/dev/null":
                current_file = None
            else:
                current_file = path_part
                modified[current_file] = set()
        elif current_file is not None and not in_header:
            if line.startswith("+") and not line.startswith("+++"):
                new_line_num += 1
                modified[current_file].add(new_line_num)
            elif line.startswith(" "):
                new_line_num += 1
            elif line.startswith("-") and not line.startswith("---"):
                pass  # Deletions do not affect lines in the new file

    return modified


def annotate_diff(diff_text: str) -> str:
    """Annotate a unified diff with new line numbers for modified and context lines."""
    annotated_lines = []
    current_file: str | None = None
    new_line_num: int = 0
    in_header: bool = True

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            current_file = None
            annotated_lines.append(line)
        elif line.startswith("@@"):
            in_header = False
            try:
                parts = line.split()
                if len(parts) >= 3:
                    new_info = parts[2]
                    if new_info.startswith("+"):
                        new_start = int(new_info[1:].split(",")[0])
                        new_line_num = new_start - 1
            except (ValueError, IndexError):
                current_file = None
            annotated_lines.append(line)
        elif in_header and line.startswith("+++ "):
            path_part = line[4:]
            if path_part.startswith('"') and path_part.endswith('"'):
                path_part = path_part[1:-1]
            if path_part.startswith("b/"):
                current_file = path_part[2:]
            elif path_part == "/dev/null":
                current_file = None
            else:
                current_file = path_part
            annotated_lines.append(line)
        elif current_file is not None and not in_header:
            if line.startswith("+") and not line.startswith("+++"):
                new_line_num += 1
                annotated_lines.append(f"{new_line_num:5d}: {line}")
            elif line.startswith(" "):
                new_line_num += 1
                annotated_lines.append(f"{new_line_num:5d}: {line}")
            elif line.startswith("-") and not line.startswith("---"):
                annotated_lines.append(f"     : {line}")
            else:
                annotated_lines.append(line)
        else:
            annotated_lines.append(line)

    return "\n".join(annotated_lines)


def filter_diff(diff_text: str, exclude_regexes: list[re.Pattern[str]]) -> str:
    """Filter out lock files, minified files, SVG files, and other non-reviewable files from the unified diff."""
    lines = diff_text.splitlines(keepends=True)
    filtered_lines = []
    current_file_excluded = False

    for line in lines:
        if line.startswith("diff --git "):
            first_line = line.strip()
            # Find the path following "b/"
            b_idx = first_line.find(' "b/')
            if b_idx != -1:
                file_path = first_line[b_idx + 4 :].strip()
                if file_path.endswith('"'):
                    file_path = file_path[:-1]
            else:
                b_idx = first_line.find(" b/")
                if b_idx != -1:
                    words = first_line[b_idx + 3 :].split()
                    file_path = words[0] if words else ""
                else:
                    file_path = ""

            if file_path and any(rx.search(file_path) for rx in exclude_regexes):
                current_file_excluded = True
                print(f"Skipping diff section for excluded file: {log_text(file_path)}")
            else:
                current_file_excluded = False

        if not current_file_excluded:
            filtered_lines.append(line)

    return "".join(filtered_lines)


def repair_truncated_json(text: str) -> str:
    """Attempt to repair a truncated JSON string by closing open quotes, brackets, and braces."""
    in_string = False
    escape = False
    stack = []

    for char in text:
        if in_string:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = False
                continue
        else:
            if char == '"':
                in_string = True
                continue
            if char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char == "}":
                if stack and stack[-1] == "}":
                    stack.pop()
            elif char == "]":
                if stack and stack[-1] == "]":
                    stack.pop()

    repaired = text
    if in_string:
        repaired += '"'
    while stack:
        repaired += stack.pop()
    return repaired


def parse_llm_json(response_text: str) -> dict[str, Any]:
    """Parse, clean, and extract JSON output from the LLM.

    This function performs the following steps:
    1. Repairs truncated JSON payloads by automatically closing open quotes, brackets, and braces.
    2. Case-insensitively strips <think>...</think> blocks if they appear before the main JSON object.
    3. Repairs invalid JSON payloads by escaping unescaped double quotes within targeted key fields
       (e.g., 'thinking', 'summary', 'path', 'body') and cleaning up trailing commas.
    4. Extracts and parses candidate JSON substrings using a robust brace-scanning fallback to tolerate
       unclosed think blocks, extra preambles, or mismatched nested braces.
    """
    response_text = response_text.strip()
    response_text = repair_truncated_json(response_text)

    # Some reasoning models (e.g., DeepSeek-R1) output their chain of thought wrapped in <think> tags,
    # even when configured for JSON mode. We strip these tags only if they appear before the actual
    # JSON payload (determined by the first '{'), to prevent accidentally deleting <think> tags that
    # are nested inside JSON string literal values (e.g., within code comments/documentation reviewed).
    first_brace = response_text.find("{")
    think_match = re.search(r"<think>", response_text, re.IGNORECASE)
    if think_match and (first_brace == -1 or think_match.start() < first_brace):
        # Locate corresponding close tag starting from the think block
        end_match = re.search(
            r"</think>", response_text[think_match.start() :], re.IGNORECASE
        )
        if end_match:
            think_end = think_match.start() + end_match.end()
            response_text = (
                response_text[: think_match.start()] + response_text[think_end:]
            ).strip()
        # If no closing tag is found, we do not strip the think block because the brace-scanning
        # logic below will naturally find and parse the actual JSON payload.

    def clean_json(text: str) -> str:
        # Escape any unescaped double quotes inside key-value string values in a single pass
        # to prevent JSON parsing crashes when models output unescaped quotes inside their feedback.
        text = CLEAN_FIELDS_RE.sub(
            lambda m: (
                str(m.group(1))
                + re.sub(
                    r'(?<!\\)(?:\\\\)*"',
                    lambda sm: str(sm.group(0))[:-1] + r"\"",
                    str(m.group(2)),
                )
                + str(m.group(3))
            ),
            text,
        )
        # Replace trailing commas (ignoring those inside strings)
        return JSON_CLEAN_RE.sub(lambda m: m.group(1) or m.group(2), text)

    # Try direct parse first
    try:
        result = json.loads(clean_json(response_text), strict=False)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Strip markdown wrappers if present
    if response_text.startswith("```"):
        lines = response_text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
        try:
            result = json.loads(clean_json(cleaned), strict=False)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # Find the last '}' which marks the end of the JSON object.
    # Scan all '{' occurrences from left to right as possible starts of the JSON object,
    # trying to parse from each candidate start_idx to the last_brace. This is highly robust
    # against unclosed think tags, extra preambles, or curly braces inside a think/preamble block.
    last_brace = response_text.rfind("}")
    if last_brace != -1:
        brace_indices = [i for i, char in enumerate(response_text) if char == "{"]
        for start_idx in brace_indices:
            if start_idx >= last_brace:
                break
            json_candidate = response_text[start_idx : last_brace + 1]
            try:
                result = json.loads(clean_json(json_candidate), strict=False)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                continue

    raise ValueError("No valid JSON object found in LLM response")


def clean_markdown_line(line: str) -> str:
    """Clean markdown markers (headers, bold, italics, backticks) from a line."""
    line = line.strip()
    # Strip leading '#' characters and whitespace
    line = MD_HEADER_RE.sub("", line)
    # Strip leading markdown list markers (e.g., - , * , + , 1. ) and whitespace
    line = MD_LIST_RE.sub("", line)
    line = MD_NUM_LIST_RE.sub("", line)
    # Strip bold/italic wrappers e.g. **path** or *path*
    line = MD_BOLD_START_RE.sub("", line)
    line = MD_BOLD_END_RE.sub("", line)
    # Strip backticks
    line = MD_BACKTICK_START_RE.sub("", line)
    line = MD_BACKTICK_END_RE.sub("", line)
    return line.strip()


def parse_markdown_comments(
    text: str, modified_lines: dict[str, set[int]]
) -> list[dict[str, Any]]:
    """Attempt to extract file, line, and comment body from free-form markdown text."""
    comments: list[dict[str, Any]] = []
    current_file: str | None = None
    current_comment: dict[str, Any] | None = None

    # Pre-compute lowercase paths, basenames, and patterns for efficient lookups outside the line loop
    path_lookups = []
    for path in modified_lines:
        norm_path = path.lower()
        basename = os.path.basename(path).lower()
        patterns = {
            norm_path,
            basename,
            f"in {norm_path}",
            f"in {basename}",
            f"file: {norm_path}",
            f"file: {basename}",
            f"file {norm_path}",
            f"file {basename}",
            f"review of {norm_path}",
            f"review of {basename}",
            f"review for {norm_path}",
            f"review for {basename}",
            f"review {norm_path}",
            f"review {basename}",
        }
        path_lookups.append((path, norm_path, basename, patterns))

    # Pattern for line numbers
    # Matches:
    # "Line 81: ...", "Lines 42-59: ...", "[Line 81]: ...", "(Line 81) - ..."
    # Case insensitive, optional brackets/parentheses, optional whitespace, colon or hyphen separator
    line_re = re.compile(
        r"^\s*[-*]?\s*[\(\[]?[lL]ines?\s+(\d+)(?:\s*-\s*(\d+))?[\)\]]?\s*[:\-]\s*(.*)$"
    )

    for line in text.splitlines():
        cleaned = clean_markdown_line(line)
        if not cleaned:
            if current_comment:
                current_comment["body"] += "\n"
            continue

        # Check if line indicates a file path from modified_lines
        found_file = False
        cleaned_lower = cleaned.lower()
        cleaned_pat = cleaned_lower.rstrip(":").strip()

        for path, norm_path, basename, patterns in path_lookups:
            if cleaned_pat in patterns or (
                (cleaned_lower.startswith("###") or cleaned_lower.startswith("##"))
                and (norm_path in cleaned_lower or basename in cleaned_lower)
            ):
                current_file = path
                found_file = True
                if current_comment:
                    comments.append(current_comment)
                    current_comment = None
                break

        if found_file:
            continue

        # Check if it's a line comment start
        line_match = line_re.match(line)
        if line_match and current_file:
            if current_comment:
                comments.append(current_comment)
            start_line = int(line_match.group(1))
            end_line_str = line_match.group(2)
            end_line = int(end_line_str) if end_line_str else start_line
            initial_text = line_match.group(3).strip()

            current_comment = {
                "path": current_file,
                "line": end_line,
                "body": initial_text,
            }
            continue

        # If we are inside a comment, append the text
        if current_comment:
            if current_comment["body"]:
                current_comment["body"] += "\n" + line
            else:
                current_comment["body"] = line

    if current_comment:
        comments.append(current_comment)

    # Post-process: clean up trailing/leading newlines and whitespace
    for c in comments:
        c["body"] = c["body"].strip()

    return comments


def truncate_diff(diff_content: str, limit: int = 500000) -> str:
    """Truncate the diff content at the last newline character before the limit."""
    if len(diff_content) > limit:
        print(
            f"PR diff size is over {limit} bytes. Truncating diff for review payload."
        )
        last_newline = diff_content.rfind("\n", 0, limit)
        if last_newline != -1:
            return (
                diff_content[:last_newline]
                + f"\n\n[Diff truncated due to size limit of {limit} bytes]"
            )
        else:
            return (
                diff_content[:limit]
                + f"\n\n[Diff truncated due to size limit of {limit} bytes]"
            )
    return diff_content


def _read_review_instruction(post_summary: bool) -> str:
    """Read the system prompt for the review, with or without a summary."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    instruction_file = (
        "system_instruction.md"
        if post_summary
        else "system_instruction_comments_only.md"
    )
    try:
        with open(
            os.path.join(script_dir, instruction_file), "r", encoding="utf-8"
        ) as f:
            return f.read()
    except Exception as e:
        print(f"Error reading {instruction_file}: {e}", file=sys.stderr)
        sys.exit(1)


def _review_prompt(diff_content: str) -> str:
    """The user message for a review: the annotated diff and the JSON-only demand."""
    return (
        # Explicitly request raw JSON in the prompt as a fallback for models/endpoints
        # that ignore the response_format API parameter or wrap JSON in markdown blocks.
        f"Please review this annotated diff:\n\n{annotate_diff(diff_content)}\n\n"
        "CRITICAL: You must respond ONLY with a valid JSON object matching the schema. "
        "Do not include any introductory or concluding text, markdown wrappers, "
        "or any content outside of the JSON object itself."
    )


def make_openrouter_request(
    api_key: str,
    diff_content: str,
    post_summary: bool = True,
    model: str = "openrouter/free",
    base_url: str = "https://openrouter.ai/api/v1/chat/completions",
    max_tokens: int = 4096,
    repo: str = "pr-code-review-action",
) -> str:
    """Call OpenRouter API to review the diff content using the specified model."""
    return openrouter_chat(
        api_key,
        _read_review_instruction(post_summary),
        _review_prompt(diff_content),
        model=model,
        base_url=base_url,
        max_tokens=max_tokens,
        repo=repo,
    )


def make_anthropic_request(
    api_key: str,
    diff_content: str,
    post_summary: bool = True,
    model: str = "claude-haiku-4-5",
    base_url: str = "https://api.anthropic.com/v1/messages",
    max_tokens: int = 8192,
    use_oauth: bool = False,
) -> str:
    """Call the Anthropic Messages API to review the diff content using the specified model.

    `api_key` is an Anthropic API key or, when `use_oauth` is true, a Claude Pro/Max
    subscription OAuth token; see `anthropic_chat`.
    """
    return anthropic_chat(
        api_key,
        _read_review_instruction(post_summary),
        _review_prompt(diff_content),
        model=model,
        base_url=base_url,
        max_tokens=max_tokens,
        use_oauth=use_oauth,
    )


def submit_github_review(
    repo: str, pr_number: str, token: str, summary: str, comments: list[dict[str, Any]]
) -> dict[str, Any]:
    """Post the review comments and summary to GitHub using the Pull Requests API."""
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }

    payload = {
        "body": summary,
        "event": "COMMENT",
        "comments": comments,
    }

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            print("Successfully submitted code review to GitHub.")
            result = json.loads(res.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("GitHub API returned non-JSON-object response.")
            return result
    except urllib.error.HTTPError as e:
        print(
            f"GitHub HTTP Error {e.code}: "
            f"{log_text(e.read().decode('utf-8', errors='replace'))}",
            file=sys.stderr,
        )
        raise
    except Exception as e:
        print(f"GitHub Error: {log_text(e)}", file=sys.stderr)
        raise


def main() -> None:
    """Validate environment configurations, process diff content, and submit review."""
    # Load and validate environment variables
    provider = os.environ.get("PROVIDER", "auto").strip().lower()
    pr_number = os.environ.get("PR_NUMBER")
    repo = os.environ.get("REPO")
    token = os.environ.get("GH_TOKEN")
    post_summary_env = os.environ.get("POST_SUMMARY", "true")
    post_summary = post_summary_env.lower() in ("true", "1", "yes")

    check_credentials(provider)

    if not pr_number or not repo or not token:
        print(
            "Error: PR_NUMBER, REPO, and GH_TOKEN environment variables are required.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Harden input parameters to prevent URL injection/manipulation
    if not pr_number.isdigit():
        print("Error: PR_NUMBER must be numeric.", file=sys.stderr)
        sys.exit(1)
    if not re.match(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$", repo):
        print("Error: REPO must be in 'owner/repo' format.", file=sys.stderr)
        sys.exit(1)

    # Read diff file
    diff_path = "pr.diff"
    if not os.path.exists(diff_path):
        print(f"Error: PR diff file '{diff_path}' not found.", file=sys.stderr)
        sys.exit(1)

    with open(diff_path, "r", encoding="utf-8") as f:
        diff_content = f.read()

    if not diff_content.strip():
        print("PR diff is empty. Skipping review.")
        sys.exit(0)

    # Retrieve and compile exclusion patterns
    custom_excludes = os.environ.get("EXCLUDE_PATTERNS")
    exclude_regexes = get_exclude_regexes(custom_excludes)

    # Filter out lock files, generated/binary assets, etc. to prevent excessive diff size
    try:
        diff_content = filter_diff(diff_content, exclude_regexes)
    except Exception as e:
        print(
            f"Warning: Failed to filter diff: {e}. Proceeding with unfiltered diff.",
            file=sys.stderr,
        )

    if not diff_content.strip():
        print("PR diff is empty after filtering excluded files. Skipping review.")
        sys.exit(0)

    # Truncate diff if it is too large (> 500KB) to stay within LLM token limits
    is_truncated = len(diff_content) > 500000
    diff_content = truncate_diff(diff_content, 500000)

    print("Parsing modified line numbers from diff...")
    modified_lines = get_modified_lines(diff_content)

    candidates = build_provider_candidates(provider)
    max_review_attempts = 3

    def send_review(config: ProviderConfig) -> str:
        if config.name == "anthropic":
            return make_anthropic_request(
                api_key=config.api_key,
                diff_content=diff_content,
                post_summary=post_summary,
                model=config.model,
                base_url=config.base_url,
                max_tokens=config.max_tokens,
                use_oauth=config.use_oauth,
            )
        if config.name == "openrouter":
            return make_openrouter_request(
                api_key=config.api_key,
                diff_content=diff_content,
                post_summary=post_summary,
                model=config.model,
                base_url=config.base_url,
                max_tokens=config.max_tokens,
                repo=repo,
            )
        raise ValueError(f"Unsupported provider: {config.name}")

    parsed, raw_response, last_exception = request_with_fallback(
        candidates, send_review, parse_llm_json, max_attempts=max_review_attempts
    )
    json_parsed = parsed is not None
    review_data: dict[str, Any] = parsed or {}

    if json_parsed:
        summary = review_data.get("summary", "Automated code review completed.")
        if not isinstance(summary, str):
            summary = str(summary)
        llm_comments = review_data.get("comments", [])
        if not isinstance(llm_comments, list):
            llm_comments = []
    else:
        print(
            f"Error: All {max_review_attempts} review attempts failed. Last error: {log_text(last_exception)}",
            file=sys.stderr,
        )
        if raw_response:
            print(
                "Attempting to parse comments from free-form markdown text of last response...",
                file=sys.stderr,
            )
            print("--- RAW LLM RESPONSE START ---", file=sys.stderr)
            print(log_block(raw_response), file=sys.stderr)
            print("--- RAW LLM RESPONSE END ---", file=sys.stderr)
            llm_comments = parse_markdown_comments(raw_response, modified_lines)
            summary = "Automated code review completed (JSON parsing failed, fallback comments parsed)."
        else:
            print(
                "Error: Could not retrieve review response from API.", file=sys.stderr
            )
            sys.exit(1)

    print(f"Filtering {len(llm_comments)} suggested comments against modified lines...")
    valid_comments = []
    # Comments the model wrote that were dropped because sanitizing failed or left nothing
    # (one it left blank is no loss and is not counted). The review says so, otherwise a
    # dropped comment looks like a clean result.
    withheld_comments = 0
    for comment in llm_comments:
        if not isinstance(comment, dict):
            continue
        path = comment.get("path")
        line = comment.get("line")
        body = comment.get("body")

        if not isinstance(path, str) or not isinstance(body, str) or line is None:
            continue

        try:
            line_int = int(line)
        except (ValueError, TypeError):
            continue

        # Normalize path: strip a/ or b/ prefixes and leading slashes
        norm_path = path
        if norm_path.startswith("b/"):
            norm_path = norm_path[2:]
        elif norm_path.startswith("a/"):
            norm_path = norm_path[2:]
        norm_path = norm_path.lstrip("/")

        # Enforce that the path exists in our diff and the line number is part of the changed chunk
        if norm_path not in modified_lines or line_int not in modified_lines[norm_path]:
            print(
                f"Skipped comment targeting unchanged line: {log_text(path)}:{log_text(line)}"
            )
            continue

        # The body is model output steered by the (untrusted) diff, so it is made safe
        # before it can reach the pull request thread.
        raw_body = body
        body = sanitize_or_drop(
            raw_body, MAX_COMMENT_CHARS, f"comment on {norm_path}:{line_int}"
        )
        if not body:
            # A comment the model left blank is not a loss; anything else is.
            if raw_body.strip():
                withheld_comments += 1
                reason = "sanitizing failed" if body is None else "nothing left"
                print(f"Withheld comment on {norm_path}:{line_int}: {reason}.")
            continue

        valid_comments.append(
            {
                "path": norm_path,
                "line": line_int,
                "side": "RIGHT",
                "body": body,
            }
        )

    summary_withheld = False

    # Build the final review body depending on post_summary and json_parsed
    if not json_parsed:
        final_body = (
            "Automated code review completed.\n\n"
            "> [!WARNING]\n"
            "> The structured code review summary could not be parsed as a JSON object. "
            "For security reasons (to prevent indirect prompt injection vectors), "
            "the raw review feedback is not posted on the pull request thread. "
            "Please check the GitHub Actions workflow execution logs to view the raw feedback."
        )
    elif not post_summary:
        if valid_comments:
            final_body = f"Automated code review completed. Posted {len(valid_comments)} inline comment(s) on specific lines."
        elif withheld_comments:
            # The notice below explains why there is nothing to read; "no issues" would
            # be wrong.
            final_body = "Automated code review completed."
        else:
            final_body = "Automated code review completed. No issues found."
    else:
        safe_summary = sanitize_or_drop(summary, MAX_SUMMARY_CHARS, "summary")
        summary_withheld = not safe_summary and bool(summary.strip())
        if summary_withheld:
            reason = "sanitizing failed" if safe_summary is None else "nothing left"
            print(f"Withheld summary: {reason}.")
        # A review with neither a body nor comments is rejected by GitHub, so fall back
        # to a neutral line if there is nothing to post.
        final_body = safe_summary or "Automated code review completed."

    if is_truncated and post_summary:
        final_body += (
            "\n\n> [!WARNING]\n> **Note**: The pull request diff was truncated "
            "because it exceeded the size limit (500KB). Some changes or files "
            "might not have been fully reviewed."
        )

    notices = []
    if withheld_comments:
        notices.append(
            f"{withheld_comments} review comment(s) were withheld because they could "
            "not be made safe to post. See the workflow log for details."
        )
    if summary_withheld:
        notices.append(
            "The review summary was withheld because it could not be made safe to "
            "post. See the workflow log for details."
        )
    if notices:
        final_body += "\n\n> [!WARNING]\n" + "\n".join(f"> {n}" for n in notices)
        # Static text and numbers only: model text must never be printed where a line
        # could start with "::" and be read as a workflow command.
        print(
            f"::warning::Review output was withheld ({withheld_comments} comment(s), "
            f"summary withheld: {'yes' if summary_withheld else 'no'}). See the log."
        )

    # Always submit a review so the user receives the status and summary feedback
    print(f"Submitting review to GitHub with {len(valid_comments)} inline comments...")
    submit_github_review(repo, pr_number, token, final_body, valid_comments)

    if not json_parsed:
        print(
            "Error: JSON parsing of LLM response failed. Exiting with failure status.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
