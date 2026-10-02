"""Ports used by bounded acquisition and split services."""

from typing import Protocol

from ..domain.extraction import ExtractionPlan
from ..domain.integrity import MediaErrors, WriteOffsetMap


class RedumperPort(Protocol):
    def dump(self, plan: ExtractionPlan, **options):
        ...

    def refine(self, plan: ExtractionPlan, **options):
        ...

    def split(self, plan: ExtractionPlan, **options):
        ...


class IntegrityParser(Protocol):
    def media_errors(self, output: str) -> MediaErrors:
        ...

    def write_offsets(self, output: str) -> WriteOffsetMap:
        ...


class StateInspector(Protocol):
    def inspect(self, state_path, tracks, offsets):
        ...
