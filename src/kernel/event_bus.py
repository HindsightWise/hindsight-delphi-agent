"""Immutable event bus coordinating vertical-slice bricks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable


def freeze_value(value: Any) -> Any:
    """Recursively freeze dicts/lists/sets into immutable read-only structures."""
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, dict):
        return MappingProxyType({k: freeze_value(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze_value(v) for v in value)
    if isinstance(value, set):
        return frozenset(freeze_value(v) for v in value)
    return value


def unfreeze_value(value: Any) -> Any:
    """Convert frozen event payloads back into plain JSON-serializable Python types."""
    if isinstance(value, MappingProxyType):
        return {k: unfreeze_value(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [unfreeze_value(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(unfreeze_value(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class Event:
    topic: str
    payload: MappingProxyType[str, Any]
    source_brick: str
    correlation_id: str
    emitted_at_utc: str


class ImmutableEventBus:
    """Synchronous pub/sub bus with frozen payloads and immutable audit log."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[[Event], None]]] = {}
        self._history: list[Event] = []

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    def emit(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        source_brick: str,
        correlation_id: str,
    ) -> Event:
        event = Event(
            topic=topic,
            payload=freeze_value(dict(payload)),
            source_brick=source_brick,
            correlation_id=correlation_id,
            emitted_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._history.append(event)
        for handler in list(self._handlers.get(topic, ())):
            handler(event)
        return event

    def history(self, topic: str | None = None) -> tuple[Event, ...]:
        if topic is None:
            return tuple(self._history)
        return tuple(ev for ev in self._history if ev.topic == topic)
