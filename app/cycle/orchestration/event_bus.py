"""
Event Bus for the asynchronous Event-Driven Swarm architecture.
Allows decoupled agents to communicate via Pub/Sub without waiting on rigid phases.
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List

logger = logging.getLogger(__name__)

class EventBus:
    def __init__(self):
        # A dictionary mapping event_name -> list of subscriber callbacks
        self._subscribers: Dict[str, List[Callable[[Any], Coroutine]]] = {}
        # The underlying asyncio queue to process events asynchronously
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def subscribe(self, event_name: str, callback: Callable[[Any], Coroutine]):
        """Subscribe an async callback to an event type."""
        if event_name not in self._subscribers:
            self._subscribers[event_name] = []
        self._subscribers[event_name].append(callback)
        logger.debug(f"[EventBus] Subscribed to {event_name}")

    def publish(self, event_name: str, payload: Any):
        """Publish an event to the bus. Returns immediately."""
        self._queue.put_nowait((event_name, payload))
        logger.debug(f"[EventBus] Published {event_name}")

    async def _process_events(self):
        """Background worker that pulls events off the queue and calls subscribers."""
        while True:
            try:
                event_name, payload = await self._queue.get()
                subscribers = self._subscribers.get(event_name, [])
                
                # Execute all subscribers concurrently for this event
                if subscribers:
                    tasks = [asyncio.create_task(sub(payload)) for sub in subscribers]
                    # We don't await them here to avoid blocking the event loop
                    # But we could if we wanted strict ordering
                    
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[EventBus] Error processing event: {e}")

    def start(self):
        """Start the background event processor."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._process_events())

    def stop(self):
        """Stop the background event processor."""
        if self._task:
            self._task.cancel()
            self._task = None

    def clear(self):
        """Clear all subscribers and pending events. Useful for tests or resetting cycles."""
        self._subscribers.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

# Global singleton for the application
event_bus = EventBus()
