"""Guard against reintroducing stored XSS in the console.

Rows used to be built by interpolating log fields into `innerHTML`. Those fields
are untrusted: `external_user_id` is free-form caller input (a stats key, never a
trust boundary), `request_text` is the caller's prompt and `fallback_reason`
quotes a provider's error message. The console sits behind HTTP Basic auth, which
the browser replays automatically, so a stored payload would run with the
operator's credentials against every /api route.

The rule this test enforces (§16.1 of docs/UI_CONCEPT_v2.md): `innerHTML` may
only be assigned a constant (in practice `''`, to clear a container). Anything
data-driven goes through the `el()` / `row()` helpers in
`static/js/core/dom.js`, which set text as text.
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
        [
            *WEB_DIR.glob("templates/*.html"),
            *WEB_DIR.glob("static/*.js"),
            *WEB_DIR.glob("static/js/**/*.js"),
        ]
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


def test_templates_carry_no_logic() -> None:
    """Behaviour lives in ES modules, not in `<script>` blocks inside templates.

    A template that scripts is a template that can grow string-built markup
    again; keeping the rule structural is cheaper than re-reviewing it.
    """
    offenders = []
    for path in WEB_DIR.glob("templates/*.html"):
        text = path.read_text()
        if "<script" in text and 'type="module" src=' not in text:
            offenders.append(path.name)
    assert not offenders, f"inline <script> in templates: {offenders}"


def test_pages_get_the_safe_helpers() -> None:
    """Every page module builds its nodes through core/dom.js."""
    dom = (WEB_DIR / "static" / "js" / "core" / "dom.js").read_text()
    assert "export function el(" in dom
    assert "export function row(" in dom
    pages = list((WEB_DIR / "static" / "js" / "pages").glob("*.js"))
    assert pages, "no page modules found"
    for page in pages:
        assert "core/dom.js" in page.read_text(), page.name
