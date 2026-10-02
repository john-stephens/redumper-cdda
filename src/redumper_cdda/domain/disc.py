"""Immutable disc-layout domain models."""

from dataclasses import dataclass
from enum import Enum

from .errors import DomainModelError, SelectionError


class TrackKind(str, Enum):
    AUDIO = "audio"
    DATA = "data"


@dataclass(frozen=True)
class Track:
    number: int
    kind: TrackKind
    control: int
    begin_lba: int
    end_lba: int
    length_msf: str = ""
    begin_msf: str = ""

    def __post_init__(self):
        if self.end_lba <= self.begin_lba:
            raise DomainModelError(
                f"Track {self.number} has a non-positive sector range."
            )

    @property
    def length_sectors(self):
        return self.end_lba - self.begin_lba


@dataclass(frozen=True)
class DiscLayout:
    tracks: tuple
    lead_out_lba: int

    def __post_init__(self):
        if not self.tracks:
            raise DomainModelError("A disc layout must contain at least one track.")
        if self.lead_out_lba != self.tracks[-1].end_lba:
            raise DomainModelError("Disc lead-out does not match the final track end.")

    def track(self, number):
        for track in self.tracks:
            if track.number == number:
                return track
        raise SelectionError(f"Track {number} was not found.")

    def audio_tracks(self):
        return tuple(
            track for track in self.tracks if track.kind is TrackKind.AUDIO
        )


@dataclass(frozen=True)
class AudioLayout:
    tracks: tuple

    def __post_init__(self):
        if not self.tracks:
            raise DomainModelError("An audio layout must contain at least one track.")
