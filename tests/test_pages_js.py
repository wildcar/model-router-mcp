"""Run the console's page modules against a stub DOM.

There is no browser here and no bundler in this project, so without this the
page modules would only ever be checked by hand — and the mistakes they make are
the boring kind a machine catches instantly: an element id that does not exist
in the template, a handler bound to a missing node, a page reading a field the
API does not send.

The harness lives in `tests/js/`; this wrapper is what makes it part of the
build. It skips when node is absent, because node is a dev convenience here and
the production host deliberately has none.

Playwright arrives in wave 2 for what a stub cannot see: layout, focus order,
real clicks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parent / "js" / "run-pages.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_page_modules_render_against_fixtures() -> None:
    result = subprocess.run(
        ["node", str(RUNNER)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=RUNNER.parent.parent.parent,
    )
    assert result.returncode == 0, result.stdout + result.stderr
