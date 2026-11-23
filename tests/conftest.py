"""Fixtures shared by the tests."""

import os
from collections.abc import Callable

import pytest
from render_oracle import CMARK, cmark_live_elements, render_markdown


@pytest.fixture
def require_cmark() -> None:
    """Fail in CI, where a missing oracle would silently turn the checks off; skip locally."""
    if CMARK is None:
        if os.environ.get("CI"):
            pytest.fail(
                "cmark-gfm is required for the sanitizer tests in CI (shell.nix)"
            )
        pytest.skip("cmark-gfm not found; it is provided by the nix shell")


@pytest.fixture
def live_elements(require_cmark: None) -> Callable[[str], list[str]]:
    return lambda markdown: cmark_live_elements(render_markdown(markdown))
