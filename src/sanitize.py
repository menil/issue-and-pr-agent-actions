"""Make text written by a model, or quoted from an API, safe to post and to log.

A pull request diff or an issue is written by someone we do not trust, and it steers
what the model writes, so nothing the model writes is posted as written: `sanitize_llm_text`
rewrites it so that GitHub renders nothing active, and `sanitize_or_drop` fails closed. The
log helpers keep quoted text from starting a log line with `::`, which GitHub Actions
reads as a workflow command. Shared by every action in this repository.
"""

import re
import sys
import traceback
import unicodedata


def log_text(value: object) -> str:
    """Render text that did not come from this program as one inert line for the log.

    GitHub Actions reads a log line that starts with `::` as a workflow command, and
    exception messages and API responses quote what the model, or a server answering
    for it, wrote: a message such as "x\\n::stop-commands::..." would otherwise put a
    command at the start of a line of its own. Escaping every line break keeps whatever
    the text says on the line of the fixed message around it, where it is only text.
    """
    return repr(str(value))[1:-1]


def log_block(text: str) -> str:
    """Render multi-line text for the log, with a prefix on every line.

    The prefix stops any line from starting with `::`. It is not whitespace, because the
    runner ignores leading whitespace before it looks for a command.
    """
    return "\n".join(f"| {line}" for line in text.splitlines())


# The diff is attacker-controlled input to the model, so everything the model writes
# can be steered by it. These limits keep a manipulated model from flooding the thread;
# GitHub itself would accept far more.
MAX_COMMENT_CHARS = 4000
MAX_SUMMARY_CHARS = 10000

# Info strings kept on code fences. GitHub gives some of them special behaviour
# (`suggestion` becomes a one-click "Commit suggestion" button; `mermaid`, `math`,
# `geojson`, `topojson` and `stl` render embedded content), so an allowlist of plain
# language names is safer than a denylist that new GitHub features would outgrow.
_FENCE_LANGUAGES = frozenset(
    {
        "bash", "c", "cpp", "cs", "csharp", "css", "diff", "dockerfile", "elixir",
        "go", "haskell", "hs", "html", "ini", "java", "javascript", "js", "json",
        "jsx", "kotlin", "lua", "makefile", "markdown", "md", "nix", "patch", "perl",
        "php", "powershell", "py", "python", "r", "rb", "ruby", "rs", "rust", "scala",
        "scss", "sh", "shell", "sql", "swift", "text", "toml", "ts", "tsx",
        "typescript", "xml", "yaml", "yml", "zsh",
    }
)  # fmt: skip

# Fences are only recognised when the opener is at column 0, which no surrounding
# markdown can change the meaning of. Closers are matched as eagerly as the renderer
# does (0-3 spaces of indent), so we can never think a block is still open after
# GitHub has closed it. Any doubt resolves towards treating text as prose, which only
# over-escapes. That only holds if no prose line can open a fence for GitHub: an opener
# indented 1-3 spaces still counts as one there, so prose escapes both fence characters
# (backtick and `~`) and such a line can never open anything.
_FENCE_OPEN_RE = re.compile(r"^(?:(`{3,})([^`]*)|(~{3,})(.*))$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

# Everything in prose that either needs escaping or must be shown as code. Mentions,
# cross-repository references, URLs and emails cannot be defused by an escape: GitHub
# acts on the rendered text, where an escape is already gone, so they are shown as
# inline code, which GitHub never links or notifies. A span the model wrote is matched
# first, so its backticks are never taken for stray ones; it cannot contain a backtick
# (`[^`]+` stops at the next one, so cost stays linear), which is what lets every span
# we write have one backtick at each end. URLs stop at a pipe, which would otherwise end
# up inside a span (see _neutralize_token). Keeping the whole pattern linear on
# long inputs needs one of two tricks. The email lookbehind only starts a match at the
# beginning of a token. A lookbehind cannot be used for cross-repository references,
# because chained ones (`a/b#1c/d#2`) must each be wrapped, so their quantifiers are
# bounded by GitHub's own limits (owner 39, repository 100 characters), which makes the
# cost of every start position constant.
#
# A mention needs a letter or digit after the `@`, but GitHub's autolinker accepts a
# domain starting with `.`, `-` or `_` (`a@.example.com`). The email alternative cannot
# start right after another token either (its lookbehind), so such an address would reach
# GitHub unwrapped in two ways. The `at` alternative catches what is left: it shows the
# `@` and everything that could continue the domain as code. The address then has no `@`
# in its text for the autolinker to find, so it does not need to be matched as an address.
#
# Three things GitHub renders that a Markdown parser does not, found by sending the
# sanitized corpus through GitHub's own renderer, each shown as code like the rest:
# - `$`: GitHub looks for math (`$x$`, `$$x$$`, $`x`$) before it parses Markdown, so
#   even an escaped `$` or an entity still opens it, and `\color{white}` inside would hide
#   text. Math is not rendered inside code, so every run of `$` becomes its own span.
# - `[!NOTE]` and its relatives: GitHub makes a quote whose first line is one an alert,
#   after unescaping, so the escape on `[` does not stop a forged "Verified" alert.
# - `#12` and `GH-12`: links to an issue or pull request of the same repository (the
#   cross-repository form is the xref pattern). GitHub does not link one that touches a
#   letter or digit, but wrapping its neighbour in code changes what it touches (`#12#13`
#   leaves `#13` linked once `#12` is code), so no boundary rule is safe: wrap them all.
_PROSE_TOKEN_RE = re.compile(
    r"(?P<span>`[^`]+`)"
    r"|(?P<url>(?:https?://|ftp://|www\.|mailto:|xmpp:)[^\s<>`|]+)"
    r"|(?P<email>(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"
    r"|(?P<xref>[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}#\d+)"
    r"|(?P<mention>@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9_.-]+)?)"
    r"|(?P<at>@[\w.-]+)"
    r"|(?P<alert>\[![A-Za-z]+\])"
    r"|(?P<ref>(?:#|GH-)[0-9]+)"
    r"|(?P<dollar>\$+)"
    r"|(?P<char>[\\`~&<\[\]|])",
    re.IGNORECASE,
)
_ESCAPED_CHARS = {"&": "&amp;", "<": "&lt;"}

# "Cs" is the unpaired surrogates: a valid pair is already one character by the time the
# model's JSON has been parsed, so a lone one is never real text. It cannot be encoded as
# UTF-8 either, which would make anything that writes the comment out fail on it.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# Invisible characters outside those categories that can still hide text: the combining
# grapheme joiner and the Hangul and Braille fillers.
_OTHER_INVISIBLE = frozenset("͏ᅟᅠ⠀ㅤﾠ")


def _is_invisible(ch: str) -> bool:
    code = ord(ch)
    return (
        unicodedata.category(ch) in _INVISIBLE_CATEGORIES
        or ch in _OTHER_INVISIBLE
        # Variation selectors: used to smuggle bytes after an emoji.
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
    )


def _strip_invisible_chars(text: str) -> str:
    """Drop control, format and other invisible characters, keeping newlines and tabs.

    These render as nothing for a human but are readable by any AI agent that later
    consumes the comment, so they are a way to smuggle hidden instructions. Zero-width
    joiners are removed too, which splits emoji sequences and affects some Persian and
    Indic text; that is accepted for English-centric code review.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch in "\n\t" or not _is_invisible(ch))


# Output beyond GitHub's 65,536 characters makes it reject the whole review. Every
# construct the rewriting below produces grows by at most this factor (the worst case is
# `&` becoming `&amp;`), and `sanitize_llm_text` enforces it, so a mistake in that
# arithmetic withholds one comment instead of losing the whole review.
_MAX_GROWTH_FACTOR = 5
# What `sanitize_llm_text` may add on top of that (marker and closing fence).
_OUTPUT_SLACK = 64


def _neutralize_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if match.lastgroup == "char":
        return _ESCAPED_CHARS.get(token, "\\" + token)
    if match.lastgroup == "span":
        if "|" not in token:
            return token
        # GitHub splits a table cell at every unescaped pipe before it reads code spans,
        # so a pipe inside a span could cut it in two and leave the rest as live markup.
        # A table needs no pipe to start, only a line of text and a `:-` line, so
        # escaping prose pipes cannot rule that out. No span we write may hold a pipe,
        # so this one is shown as escaped text instead. Its backticks are escaped
        # too, which keeps it from pairing with the next span on the line.
        return "\\`" + _neutralize_prose_line(token[1:-1]) + "\\`"
    return f"`{token}`"


def _neutralize_prose_line(line: str) -> str:
    """Rewrite one line of prose so that no part of it can render as an active element.

    Nothing here tries to recognise links, images or HTML. Instead every character that
    can begin one is escaped, so those constructs cannot form at all, and the few things
    GitHub acts on after rendering are shown as code. Escaping `\\`, backticks and `~`
    matters as well: otherwise a stray one could pair with, or cancel, the code spans we
    write, or open a fence that our block tracking does not know about.

    Every backtick left in the output is either escaped or part of a span with one
    backtick at each end and no backtick or pipe inside, so the output never has a run
    of three backticks or a span that something else could re-pair, and safety does not
    depend on pairing spans the way GitHub does. Code containing a backtick or a pipe
    is therefore shown as escaped text, not as code. A table can still form without any
    pipe; that is harmless, because nothing active can be inside one of its cells.
    """
    return _PROSE_TOKEN_RE.sub(_neutralize_token, line)


def _safe_fence_opener(run: str, info: str) -> str:
    """Rebuild a fence opener, keeping only an allowlisted language name."""
    words = info.split()
    if not words:
        return run
    language = words[0].lower()
    return run + (language if language in _FENCE_LANGUAGES else "text")


class SanitizeError(ValueError):
    """Sanitized text broke a bound. The message holds only numbers, so it can be logged."""


def sanitize_llm_text(text: str, max_chars: int) -> str:
    """Make model-written markdown safe to post on a pull request.

    The model's output is steered by the PR diff, which an attacker controls, so a
    prompt-injected model can try to ping users, plant links or images, hide text from
    reviewers, or offer a malicious one-click suggestion. This enforces the safe form
    deterministically instead of relying on the model to refuse.

    Apart from the bodies of fenced code blocks, which cannot render anything active,
    the text is rewritten line by line (see _neutralize_prose_line) and fence openers are
    rebuilt with an allowlisted language. The visible text is capped before sanitizing
    and the result is at most _MAX_GROWTH_FACTOR times that cap (`&` becomes `&amp;`),
    which this enforces: a longer result raises SanitizeError instead of being returned.
    """
    # Strip before capping: otherwise a run of invisible characters at the start would
    # use up the cap, push the visible text out of it, and get the whole comment withheld.
    text = _strip_invisible_chars(text).strip()
    truncated = len(text) > max_chars
    text = text[:max_chars].rstrip()

    lines: list[str] = []
    fence: tuple[str, int] | None = None  # (fence character, opener length)
    for line in text.split("\n"):
        if fence:
            closer = _FENCE_CLOSE_RE.match(line)
            if (
                closer
                and closer.group(1)[0] == fence[0]
                and len(closer.group(1)) >= fence[1]
            ):
                lines.append(closer.group(1))
                fence = None
            else:
                lines.append(line)
            continue
        opener = _FENCE_OPEN_RE.match(line)
        if opener:
            run = opener.group(1) or opener.group(3)
            info = opener.group(2) if opener.group(1) else opener.group(4)
            lines.append(_safe_fence_opener(run, info))
            fence = (run[0], len(run))
        else:
            lines.append(_neutralize_prose_line(line))

    if fence:
        # Close the block even when the text was not cut: whatever is appended after it
        # (the truncation marker below, and the notices main() adds to the review) would
        # otherwise be swallowed into the code, where a warning reads as plain text.
        lines.append(fence[0] * fence[1])
    if truncated:
        lines.extend(["", "_(truncated)_"])
    result = "\n".join(lines)
    # Enforce the bound the caps are sized for, so a mistake in the growth arithmetic
    # withholds one comment instead of making GitHub reject the whole review.
    limit = _MAX_GROWTH_FACTOR * max_chars + _OUTPUT_SLACK
    if len(result) > limit:
        raise SanitizeError(f"sanitized text is {len(result)} characters, over {limit}")
    return result


def _describe_failure(error: Exception) -> str:
    """Name a sanitizer failure for the log without repeating its message.

    The text being sanitized is attacker-influenced and exception messages often quote
    their input, so a message could put model text at the start of a log line, where a
    leading `::` is read as a workflow command. Only our own SanitizeError, whose message
    is built from numbers, is shown in full. Anything else gets its type and where it was
    raised, which is what is needed to debug it.
    """
    if isinstance(error, SanitizeError):
        return f"{type(error).__name__}: {error}"
    frames = traceback.extract_tb(error.__traceback__)
    where = f" at {frames[-1].name}:{frames[-1].lineno}" if frames else ""
    return f"{type(error).__name__}{where}"


def sanitize_or_drop(text: str, max_chars: int, what: str) -> str | None:
    """Sanitize model text for posting, failing closed.

    If sanitizing fails for any reason the text is dropped (None), never posted as
    written, because the unsanitized text is exactly what must not reach the thread.
    An empty string is different: sanitizing worked and nothing visible was left. The
    caller treats both as withheld, but the log says which. Over-long input and failures
    are logged so a missing comment can be explained.
    """
    if len(text) > max_chars:
        # Invisible characters are removed before the cap applies, so the text may not
        # actually be cut; say what was measured and what the limit is, nothing more.
        print(
            f"Long {what}: {len(text)} characters in, limit is {max_chars} once "
            "invisible characters are removed."
        )
    try:
        return sanitize_llm_text(text, max_chars)
    except Exception as e:  # noqa: BLE001 - deliberately broad: any failure must fail closed
        print(
            f"Error: could not sanitize {what}; dropping it: {_describe_failure(e)}",
            file=sys.stderr,
        )
        return None
