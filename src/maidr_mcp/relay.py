"""Hands the model's calls to the chart view that runs them.

No host passes a view's own tools to the model yet (ext-apps #797), so each
model-facing ``maidr_*`` tool queues its call here. The chart's view long-polls
for it through an app-only tool, runs it on maidr.js's own WebMCP tool, and
posts maidr's answer back, which becomes the model's tool result.

A ``viewId`` is the only key to a chart: a model call names one, and nothing
falls back to "the only open chart", which on a shared server could be another
user's. Everything lives in this process's memory, so the server runs as one
instance.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import anyio


@dataclass
class Call:
    call_id: str
    tool: str
    arguments: dict[str, Any]


@dataclass
class _View:
    svg: str | None = None
    queue: list[Call] = field(default_factory=list)
    wake: anyio.Event | None = None  # set by a call; made by the poll waiting for it
    seen: float = field(default_factory=time.monotonic)


@dataclass
class _Waiting:
    view_id: str
    done: anyio.Event = field(default_factory=anyio.Event)
    result: dict[str, Any] | None = None


class Relay:
    """Per-view queues of model calls, and the answers coming back.

    Args:
        poll_seconds: How long a view's poll waits for a call before returning empty.
        reply_seconds: How long a model call waits for the view's answer.
        idle_seconds: How long a view may go without polling before it is forgotten.
        max_views: The most views held at once.
        max_queue: The most calls waiting for one view.
    """

    def __init__(
        self,
        *,
        poll_seconds: float = 20.0,
        reply_seconds: float = 10.0,
        idle_seconds: float = 900.0,
        max_views: int = 10_000,
        max_queue: int = 16,
    ) -> None:
        self.poll_seconds = poll_seconds
        self.reply_seconds = reply_seconds
        self.idle_seconds = idle_seconds
        self.max_views = max_views
        self.max_queue = max_queue
        self._views: dict[str, _View] = {}
        self._waiting: dict[str, _Waiting] = {}

    def open(self, svg: str | None = None) -> str:
        """Register a new chart view and return its ``viewId``.

        Raises:
            RuntimeError: if the server already holds ``max_views`` views.
        """
        self._sweep()
        if len(self._views) >= self.max_views:
            raise RuntimeError("too many charts are open on this server")
        view_id = secrets.token_urlsafe(18)
        self._views[view_id] = _View(svg=svg)
        return view_id

    def svg(self, view_id: str) -> str | None:
        """The SVG a view was opened with, while the server still holds it."""
        view = self._views.get(view_id)
        return view.svg if view else None

    async def call(self, view_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run ``tool`` in the view and return maidr's answer, or an error the model can act on."""
        view = self._views.get(view_id)
        if view is None:
            return {
                "ok": False,
                "error": "unknown viewId: the chart is closed, or the server restarted. "
                "Show it again.",
            }
        if len(view.queue) >= self.max_queue:
            return {"ok": False, "error": "too many calls are waiting for this chart"}
        call = Call(secrets.token_urlsafe(12), tool, arguments)
        waiting = self._waiting[call.call_id] = _Waiting(view_id)
        view.queue.append(call)
        if view.wake is not None:
            view.wake.set()
        try:
            with anyio.move_on_after(self.reply_seconds):
                await waiting.done.wait()
        finally:
            # Also when the model's request is cancelled mid-wait: nothing may linger.
            self._waiting.pop(call.call_id, None)
            if waiting.result is None and call in view.queue:
                view.queue.remove(call)  # never picked up: a returning view must not run it late
        if waiting.result is not None:
            return waiting.result
        return {
            "ok": False,
            "error": "the chart did not answer. It may have been closed, or scrolled out of the "
            "conversation; ask the reader to bring it back into view.",
        }

    async def poll(self, view_id: str) -> list[Call]:
        """The calls waiting for a view, after waiting up to ``poll_seconds`` for one.

        A view the server does not know, after a restart, is taken back: it holds the id.
        """
        view = self._views.get(view_id)
        if view is None:
            self._sweep()
            if len(self._views) >= self.max_views:
                return []
            view = self._views[view_id] = _View()
        view.seen = time.monotonic()
        if not view.queue:
            view.wake = anyio.Event()
            with anyio.move_on_after(self.poll_seconds):
                await view.wake.wait()
        view.seen = time.monotonic()
        calls, view.queue = view.queue, []
        return calls

    def reply(self, view_id: str, call_id: str, result: dict[str, Any]) -> bool:
        """Deliver a view's answer to the model call waiting on it."""
        waiting = self._waiting.get(call_id)
        if waiting is None or waiting.view_id != view_id or waiting.done.is_set():
            return False
        waiting.result = result
        waiting.done.set()
        return True

    def _sweep(self) -> None:
        cutoff = time.monotonic() - self.idle_seconds
        for view_id in [
            v for v, view in self._views.items() if view.seen < cutoff and not view.queue
        ]:
            del self._views[view_id]
