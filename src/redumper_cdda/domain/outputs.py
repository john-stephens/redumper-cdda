"""Immutable output-planning and result models."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class OutputKind(str, Enum):
    AUDIO = "audio"
    DATA = "data"


@dataclass(frozen=True)
class AudioSegment:
    path: Path
    track_number: int
    start_sector: int
    sectors: int
    bin_sectors: int


@dataclass(frozen=True)
class DataTrackSource:
    cue_path: Path
    path: Path
    track_number: int
    track_type: str
    sector_size: int
    start_sector: int
    sectors: int


@dataclass(frozen=True)
class CueTrack:
    number: int
    track_type: str
    file_name: object
    indexes: tuple

    def index(self, number):
        for index_number, sector in self.indexes:
            if index_number == number:
                return sector
        return None


@dataclass(frozen=True)
class OutputPlan:
    track: object
    kind: OutputKind
    component_tracks: tuple
    expected_sectors: int
    output_path: Path


@dataclass(frozen=True)
class ResolvedOutput:
    plan: OutputPlan
    audio_segments: tuple = ()
    data_source: object = None
    cue_path: object = None
    pregap_skipped: object = None


@dataclass(frozen=True)
class OmittedOutput:
    plan: OutputPlan
    media_errors: object


@dataclass(frozen=True)
class VerificationTrack:
    track: object
    audio_segments: tuple


@dataclass(frozen=True)
class CompletedOutput:
    plan: OutputPlan
    path: Path


@dataclass(frozen=True)
class SplitResult:
    outputs: tuple
    omitted: tuple = ()
    verification_tracks: tuple = ()
    media_errors: object = None


@dataclass(frozen=True)
class VerificationReport:
    disc_id: str
    results: tuple
