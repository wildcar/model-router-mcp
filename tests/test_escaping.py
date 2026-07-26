"""Guard against reintroducing stored XSS in the console.

Rows used to be built by interpolating log fields into `innerHTML`. Those fields
are untrusted: `external_user_id` is free-form caller input (a stats key, never a
trust boundary), `request_text` is the caller's prompt and `fallback_reason`
quotes a provider's error message. The console sits behind HTTP Basic auth, which
the browser replays automatically, so a stored payload would run with the
operator's credentials against every /api route.

The rule this test enforces: `innerHTML` may only be assigned a constant (in
practice `''`, to clear a container). Anything data-driven goes through the
`el()` / `row()` helpers in static/app.js, which set text as text.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "router_mcp" / "web"

ASSIGNMENT = re.compile(r"innerHTML\s*=")
# The one allowed form: clearing a container.
CLEARING = re.compile(r"""innerHTML\s*=\s*(''|""|``)\s*;?\s*$""")


def _js_sources() -> list[Path]:
    return sorted(
        [*WEB_DIR.glob("templates/*.html"), *WEB_DIR.glob("static/*.js")]
    )


def test_no_data_driven_inner_html() -> None:
    offenders: list[str] = []
    for path in _js_sources():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("//"):
                continue
            if ASSIGNMENT.search(line) and not CLEARING.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "innerHTML assigned something other than an empty literal; build nodes "
        "with el()/row() instead:\n" + "\n".join(offenders)
    )


def test_helpers_are_available_to_every_page() -> None:
    """The helpers live in app.js, which base.html loads before page scripts."""
    app_js = (WEB_DIR / "static" / "app.js").read_text()
    assert "function el(" in app_js
    assert "function row(" in app_js
    base = (WEB_DIR / "templates" / "base.html").read_text()
    assert base.index("/static/app.js") < base.index("{% block script %}")
