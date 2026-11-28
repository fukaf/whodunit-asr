"""Server-sent events: a thread-safe fan-out that also replays history.

One audio job lives on a worker thread; several browser tabs may watch it at
once (the job page, a second monitor, a reload mid-run). Each subscriber gets
its own queue plus everything already emitted, so a client that connects after
a job started still sees the whole timeline instead of starting at "40 %".

Kept dependency-free on purpose: `queue.Queue` + a lock is all this needs, and
it stays testable without any HTTP stack.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import Any, Iterator


class EventBus:
    """Append-only event log with per-subscriber cursors."""

    def __init__(self, history: int = 200):
        self._lock = threading.Lock()
        self._log: deque[dict[str, Any]] = deque(maxlen=history)
        self._subs: set[queue.Queue] = set()
        self._closed = False

    # -- publishing --------------------------------------------------------
    def publish(self, event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {
            "seq": len(self._log) + 1,
            "ts": round(time.time(), 3),
            "event": event,
            "data": data or {},
        }
        with self._lock:
            # `seq` restarts at len+1 on a full deque; the monotonic clock stamp
            # is what clients use to order, `seq` is only a cheap identity.
            record["seq"] = (self._log[-1]["seq"] + 1) if self._log else 1
            self._log.append(record)
            subs = list(self._subs)
        for q in subs:
            q.put(record)
        return record

    def close(self) -> None:
        """Tell every subscriber the stream is over (`None` sentinel)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subs = list(self._subs)
            self._subs.clear()
        for q in subs:
            q.put(None)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    # -- subscribing -------------------------------------------------------
    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._log)

    def subscribe(self) -> "Subscription":
        q: queue.Queue = queue.Queue()
        with self._lock:
            replay = list(self._log)
            if not self._closed:
                self._subs.add(q)
            else:
                replay.append(None)          # already finished: replay then stop
        return Subscription(q, replay)

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until no subscriber is registered. Test helper."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._subs:
                    return True
            time.sleep(0.01)
        return False


class Subscription:
    """A cursor over one bus: history first, then live events."""

    def __init__(self, q: queue.Queue, replay: list[dict[str, Any] | None]):
        self._q = q
        self._replay = deque(replay)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        while self._replay:
            item = self._replay.popleft()
            if item is None:
                return
            yield item
        while True:
            item = self._q.get()
            if item is None:
                return
            yield item

    def drain(self, timeout: float = 0.0) -> list[dict[str, Any]]:
        """Everything available right now; stops at the end-of-stream sentinel."""
        out: list[dict[str, Any]] = []
        while self._replay:
            item = self._replay.popleft()
            if item is None:
                return out
            out.append(item)
        while True:
            try:
                item = self._q.get(timeout=timeout) if timeout else self._q.get_nowait()
            except queue.Empty:
                return out
            if item is None:
                return out
            out.append(item)


def format_sse(record: dict[str, Any]) -> str:
    """Encode one record as an SSE frame (id + event + JSON data)."""
    payload = json.dumps(record["data"], ensure_ascii=False)
    return f"id: {record['seq']}\nevent: {record['event']}\ndata: {payload}\n\n"
