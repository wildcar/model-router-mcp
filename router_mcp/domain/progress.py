"""A place for work in flight to say what it is doing.

The timeline the console draws is built by the router, which knows about
planning, attempts, cost and logging — but the part an operator actually waits
through happens inside an adapter: a CLI engine chewing through a repository for
two minutes, printing an event per step. Without a channel from the adapter back
out, those two minutes are one blank line labelled «attempt 1».

The channel is a ContextVar, for the same reason the HTTP retry trace is one:
adapters are called through a `generate(request, model)` signature shared by
eight implementations, and threading a progress argument through all of them
would change every adapter to serve one of them. A ContextVar is set by whoever
runs the request and is invisible to adapters that never emit.

Emitting is always safe: with no sink installed (an MCP tool call, a test) it is
a no-op, and a sink that raises must never break the run that was reporting to
it.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any, Callable

logger = logging.getLogger(__name__)

# A sink takes one step dict. The router adds the timing and stores it; the run
# registry publishes it to whoever is polling.
Sink = Callable[[dict[str, Any]], None]

_sink: ContextVar[Sink | None] = ContextVar("progress_sink", default=None)


def set_sink(sink: Sink | None) -> Token:
    return _sink.set(sink)


def reset_sink(token: Token) -> None:
    _sink.reset(token)


def emit(kind: str, title: str, **detail: Any) -> None:
    """Report one step of work in progress."""
    sink = _sink.get()
    if sink is None:
        return
    try:
        sink({"kind": kind, "title": title, **detail})
    except Exception:  # pragma: no cover - reporting must not break the run
        logger.warning("Progress sink raised", exc_info=True)
