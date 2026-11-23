"""What a rendered comment is checked for, shared by the tests of every action.

Two oracles: cmark-gfm (the parser GitHub's renderer is built on, in the nix shell) and
GitHub's own renderer, reached with `gh api markdown`, which also shows what GitHub adds
on top of it: math, alerts, issue and commit links, embedded renderers.
"""

import re
import shutil
import subprocess
import time

from sanitize import _FENCE_LANGUAGES

CMARK = shutil.which("cmark-gfm")


INERT_TAGS = {
    "p", "em", "strong", "del", "ul", "ol", "li", "blockquote", "h1", "h2", "h3",
    "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "hr", "br",
    "input", "code", "pre",
}  # fmt: skip


def render_markdown(markdown: str) -> str:
    assert CMARK
    return subprocess.run(
        [
            CMARK,
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


def cmark_live_elements(html: str) -> list[str]:
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
        if tag.lower() not in INERT_TAGS | {"a", "img"}:
            found.append(f"html:{tag}")
            break
    text = re.sub(r"<[^>]+>", " ", html)
    if re.search(r"(?<![A-Za-z0-9_])@[A-Za-z0-9]", text):
        found.append("mention")
    if re.search(r"(?<![\w/.-])[A-Za-z0-9-]+/[A-Za-z0-9_.-]+#\d+", text):
        found.append("cross-repo reference")
    return found


def github_live_elements(html: str) -> list[str]:
    """List what GitHub's own renderer makes active in its HTML, outside code: links
    (autolinks, mentions, issue and commit references), images, math, alerts, embedded
    renderers (mermaid, geojson, stl), and fences with a language that is not on our
    allowlist (`suggestion` becomes a button)."""
    found = [
        f"fence language: {language}"
        for language in re.findall(r'<pre lang="([^"]*)"', html)
        if language not in _FENCE_LANGUAGES
    ]
    if "js-render-needs-enrichment" in html:
        found.append("embedded renderer")
    html = re.sub(r"<pre\b.*?</pre>", " ", html, flags=re.DOTALL)
    html = re.sub(r"<code\b.*?</code>", " ", html, flags=re.DOTALL)
    for name, pattern in [
        ("link", r"<a[\s>]"),
        ("image", r"<img[\s>]"),
        ("math", r"<math-renderer"),
        ("alert", r"markdown-alert"),
        ("embed", r"<(?:iframe|video|details|input)[\s>]"),
    ]:
        if re.search(pattern, html):
            found.append(name)
    return found


def render_on_github(text: str) -> str:
    """Render Markdown with GitHub's own renderer (`gh api markdown`). It sends only the
    text given, to a public endpoint that creates nothing. A run is hundreds of requests,
    so rate limits and network blips are waited out instead of failing the whole run."""
    problem = ""
    for attempt in range(6):
        try:
            result = subprocess.run(
                ["gh", "api", "-X", "POST", "markdown", "-f", f"text={text}", "-f",
                 "mode=gfm", "-f", "context=octocat/Hello-World"],
                capture_output=True, text=True, timeout=60,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            problem = "timed out"
            time.sleep(5 * (attempt + 1))
            continue
        if result.returncode == 0:
            return result.stdout
        problem = result.stderr[:200]
        time.sleep(
            30 * (attempt + 1) if "rate limit" in problem.lower() else 5 * (attempt + 1)
        )
    raise RuntimeError(f"gh api markdown kept failing: {problem}")
