"""Disc-layout acquisition ports."""

from typing import Protocol

from ..domain.disc import AudioLayout, DiscLayout


class MmcLayoutReader(Protocol):
    def read(self, device: str) -> DiscLayout:
        ...


class AudioLayoutReader(Protocol):
    def read(self, device: str, verbose: bool = False) -> AudioLayout:
        ...


class LayoutProvider(Protocol):
    def read(self, device: str, verbose: bool = False) -> DiscLayout:
        ...
