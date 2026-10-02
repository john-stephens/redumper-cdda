"""Structured extraction lifecycle events."""

from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleEvent:
    name: str
    values: object = None

