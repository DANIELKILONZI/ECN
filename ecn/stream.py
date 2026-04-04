"""
stream.py
---------
Kafka-inspired event streaming backbone for ECN.

No external message broker required.  The EventBus provides:

1. **Topic-based publishing** — ``publish(topic, payload)``
2. **Ring-buffer per topic** — last ``maxlen`` events stored; safe for slow
   consumers (they can catch up via offset-based polling).
3. **Offset-based polling** — Kafka-style ``consume(topic, offset, limit)``
   lets consumers track their own position and poll on demand.
4. **SSE live subscriptions** — ``subscribe_live(topic)`` returns an
   asyncio.Queue that receives every new event for a topic in real time.

Topics
------
- ``"transactions"`` — published after every consensus round
- ``"faults"``       — published only when a fault is detected in a round

Integration
-----------
The ``EventBus`` singleton is wired into the FastAPI app in ``api.py`` and
published to after each ``POST /transactions`` completes.

REST endpoints (added to api.py):
    GET  /stream/topics
        List available topics and their event counts.
    GET  /stream/topics/{topic}?offset=0&limit=10
        Kafka-style poll: return events from the given absolute offset.
    GET  /stream/events?topics=transactions,faults&limit=N
        Server-Sent Events (SSE) stream.  ``limit`` causes auto-close after
        N events (useful for tests).  Omit for an indefinitely live stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Maximum events retained per topic (ring buffer)
_DEFAULT_MAXLEN = 1000

# Supported topics
TOPIC_TRANSACTIONS = "transactions"
TOPIC_FAULTS = "faults"
ALL_TOPICS = [TOPIC_TRANSACTIONS, TOPIC_FAULTS]


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """
    In-memory event bus with Kafka-inspired semantics.

    Each topic stores events in a ring buffer (deque with maxlen).
    Consumers can poll at their own pace using an absolute offset, or
    receive live events via asyncio queues.

    Parameters
    ----------
    maxlen : int
        Maximum number of events retained per topic.
    """

    def __init__(self, maxlen: int = _DEFAULT_MAXLEN) -> None:
        self._maxlen = maxlen
        # buffer[topic] = deque of (absolute_seq, payload) tuples
        self._buffers: Dict[str, Deque[Tuple[int, Dict[str, Any]]]] = {
            t: deque(maxlen=maxlen) for t in ALL_TOPICS
        }
        # next sequence number per topic (monotonically increasing)
        self._seq: Dict[str, int] = {t: 0 for t in ALL_TOPICS}
        # live subscriber queues per topic
        self._subscribers: Dict[str, List[asyncio.Queue]] = {
            t: [] for t in ALL_TOPICS
        }

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def publish(self, topic: str, payload: Dict[str, Any]) -> int:
        """
        Publish an event to a topic.

        Parameters
        ----------
        topic : str
            One of ``ALL_TOPICS``.
        payload : dict
            The event data (must be JSON-serialisable).

        Returns
        -------
        int
            Absolute sequence number assigned to this event.
        """
        if topic not in self._buffers:
            raise ValueError(f"Unknown topic: {topic!r}")

        seq = self._seq[topic]
        self._buffers[topic].append((seq, payload))
        self._seq[topic] = seq + 1

        # Fan-out to live subscribers (non-blocking; drop if queue full)
        dead = []
        for q in self._subscribers[topic]:
            try:
                q.put_nowait((seq, payload))
            except asyncio.QueueFull:
                logger.debug("EventBus: subscriber queue full for topic=%s, seq=%d", topic, seq)
            except Exception:
                dead.append(q)
        for q in dead:
            self._try_remove_subscriber(topic, q)

        return seq

    # ------------------------------------------------------------------
    # Offset-based polling (Kafka-style)
    # ------------------------------------------------------------------

    def consume(
        self,
        topic: str,
        offset: int = 0,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Return up to ``limit`` events starting at ``offset`` for *topic*.

        Parameters
        ----------
        topic : str
        offset : int
            Absolute event index (0-based) to start from.
        limit : int
            Maximum number of events to return.

        Returns
        -------
        list[dict]
            Each item has ``"seq"``, ``"topic"``, and ``"payload"`` keys.
        """
        if topic not in self._buffers:
            raise ValueError(f"Unknown topic: {topic!r}")

        buf = self._buffers[topic]
        if not buf:
            return []

        # The ring buffer may not contain events before its oldest entry
        oldest_seq = buf[0][0]
        results = []
        for seq, payload in buf:
            if seq < offset:
                continue
            results.append({"seq": seq, "topic": topic, "payload": payload})
            if len(results) >= limit:
                break
        return results

    def next_offset(self, topic: str) -> int:
        """Return the next sequence number that will be assigned for *topic*."""
        return self._seq.get(topic, 0)

    # ------------------------------------------------------------------
    # SSE live subscription
    # ------------------------------------------------------------------

    def subscribe_live(self, topics: List[str], maxsize: int = 256) -> asyncio.Queue:
        """
        Create a live subscription queue for one or more topics.

        The returned queue receives ``(seq, topic, payload)`` tuples as new
        events are published.  Call :py:meth:`unsubscribe_live` when done.

        Parameters
        ----------
        topics : list[str]
        maxsize : int
            Maximum items buffered in the queue before drops.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        for topic in topics:
            if topic in self._subscribers:
                self._subscribers[topic].append(q)
        return q

    def unsubscribe_live(self, q: asyncio.Queue) -> None:
        """Remove a live subscription queue from all topics."""
        for topic in ALL_TOPICS:
            self._try_remove_subscriber(topic, q)

    def _try_remove_subscriber(self, topic: str, q: asyncio.Queue) -> None:
        try:
            self._subscribers[topic].remove(q)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def topic_info(self) -> List[Dict[str, Any]]:
        """Return metadata for all topics."""
        return [
            {
                "topic": t,
                "buffered_events": len(self._buffers[t]),
                "next_offset": self._seq[t],
            }
            for t in ALL_TOPICS
        ]


# ---------------------------------------------------------------------------
# SSE response generator (used by api.py)
# ---------------------------------------------------------------------------

async def sse_generator(
    bus: EventBus,
    topics: List[str],
    limit: Optional[int] = None,
    backfill: int = 0,
):
    """
    Async generator that yields Server-Sent Event formatted strings.

    Parameters
    ----------
    bus : EventBus
    topics : list[str]
        Topics to subscribe to.
    limit : int, optional
        Stop after this many events (useful for tests; omit for indefinite stream).
    backfill : int
        Replay this many recent buffered events per topic before starting live
        streaming.  Provides Kafka-style "catch-up" semantics so reconnecting
        consumers do not miss events that were published while disconnected.
    """
    emitted = 0

    # Phase 1: replay recent buffered events (catch-up / reconnect semantics)
    if backfill > 0:
        for topic in topics:
            start = max(0, bus.next_offset(topic) - backfill)
            for ev in bus.consume(topic, offset=start, limit=backfill):
                data = json.dumps({"seq": ev["seq"], "payload": ev["payload"]})
                yield f"data: {data}\n\n"
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

    # Phase 2: live streaming via subscriber queue
    q = bus.subscribe_live(topics)
    try:
        while True:
            if limit is not None and emitted >= limit:
                break
            try:
                seq, payload = await asyncio.wait_for(q.get(), timeout=15.0)
                data = json.dumps({"seq": seq, "payload": payload})
                yield f"data: {data}\n\n"
                emitted += 1
            except asyncio.TimeoutError:
                # Heartbeat — keeps the connection alive through proxies
                yield ": heartbeat\n\n"
    finally:
        bus.unsubscribe_live(q)
