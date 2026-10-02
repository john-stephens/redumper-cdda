"""Reporting boundary for application and adapter lifecycle events."""

from typing import Protocol

from ..domain.events import LifecycleEvent


class Reporter(Protocol):
    def publish(self, event: LifecycleEvent) -> None:
        ...

