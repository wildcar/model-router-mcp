# ADR-0005: Plain ES modules for the console, no htmx or Alpine

**Status:** Accepted
**Date:** 2026-07-26
**Deciders:** Claude, with the reasoning left here for the project owner to reverse
**Scope:** `router_mcp/web/static/js/*`, `router_mcp/web/templates/*`,
`router_mcp/web/{api,views,capabilities,labels}.py`; supersedes the frontend
stack decision in §18 of `docs/UI_CONCEPT_v2.md`

---

## Context

§18 of the concept decided: stay on Jinja2 without a bundler, add htmx and
Alpine as vendored files, move all JS out of templates into native ES modules.
The reasoning was sound — no node on the production host, updates are `git pull`
plus `pip install`, the test harness is pytest — and this ADR keeps all of it
except the two libraries.

By the time wave 0 finished, the parts of that decision that carry the weight
were already in place: JS lives in ES modules under `static/js/`, templates hold
no `<script>` logic, `core/dom.js` is the only place data becomes nodes, and
`/api/v1` answers with stable envelopes. Wave 1 then built the JSON viewer, the
inspector, the two-level timeline, the cascade and the plan on top of that, and
neither library was reached for once.

htmx earns its place when the server returns HTML fragments. That is why the
concept also proposed `fragments.py` and a `/ui/*` namespace beside `/api/*`.
But `web/views.py` already renders every screen's data as one envelope, and the
inspector has to run identically in three places over that envelope. Adding
fragments would mean a second rendering path for the same facts, kept in step by
hand.

Alpine puts behaviour into markup attributes. That sits badly next to the rule
this project already enforces with a test — templates are markup, behaviour is
in modules — and it would blur the boundary the escaping guard depends on.

## Decision

The console is built from native ES modules with no frontend dependencies. No
htmx, no Alpine, no vendored bundles, no build step.

```
static/js/
  core/{api,dom,format,state,keys}.js    network, nodes, formatting, URL state, keys
  components/{json-view,inspector,timeline}.js
  pages/{run,models,logs,overview,connections}.js
```

Rules that stay from §18 and now have teeth:

- a template contains markup and one `<script type="module">` tag, nothing else
  (`tests/test_escaping.py::test_templates_carry_no_logic`);
- data becomes nodes only through `core/dom.js`; `innerHTML` may be assigned
  only an empty literal;
- a component neither fetches nor reads the address bar — page modules do both;
- domain knowledge (identifier splitting, price units, reason texts, the
  glossary) lives on the server and arrives via `/api/v1/capabilities`.

`/api/*` remains pure JSON. The `/ui/*` fragment namespace from §17.5 is not
created; if fragments are ever wanted, that namespace is still free.

## Consequences

- One rendering path. Every screen reads the same envelopes the MCP tools and
  any other API client read, so a drift between a fragment and its JSON cannot
  happen.
- No vendored blobs to re-download, re-audit and re-pin. The console has no
  supply chain.
- Harder: no declarative reactivity. Wave 3's run registry with polling will
  need explicit re-render calls where Alpine would have re-rendered by binding.
  This is a real cost, and the place to re-examine this decision.
- Harder: more hand-written wiring per screen — roughly the `addEventListener`
  block at the bottom of each page module.
- `tests/js/` runs the page modules against a stub DOM under node, so the wiring
  is checked by the build. Node stays a dev-only convenience; the test skips
  when it is absent, and the production host still needs nothing but Python.

## Alternatives considered

- **htmx + Alpine as the concept specified** — buys server-rendered fragments
  the API already covers as JSON, at the price of a second rendering path plus
  behaviour in markup attributes. Reconsider if the console ever needs to work
  without JavaScript, which nothing asks for today.
- **A bundler and a framework (Vite + Svelte/React)** — rejected in the concept
  and still rejected: the production host has no node, and deployment is two
  commands. Wave 3 revisits this by prior agreement.
- **Alpine alone, without htmx** — the declarative reactivity is genuinely
  attractive for wave 3, but it lands logic back in the templates that
  `test_escaping.py` keeps clean. If wave 3 finds the manual re-rendering
  painful, this is the first alternative to pick up.
