"""Immutable extraction request, plan, and result models."""

from dataclasses import dataclass
from pathlib import Path

from .disc import DiscLayout
from .errors import DomainModelError
from .integrity import MediaErrors


@dataclass(frozen=True)
class TrackSelection:
    start: object = None
    end: object = None


@dataclass(frozen=True)
class TrackPosition:
    """A track-relative sector position supplied by the CLI."""

    track_number: int
    offset_sectors: int

    def __post_init__(self):
        if self.track_number < 0 or self.offset_sectors < 0:
            raise DomainModelError("Track positions cannot be negative.")


@dataclass(frozen=True)
class SectorRange:
    start_lba: int
    end_lba: int

    def __post_init__(self):
        if self.end_lba <= self.start_lba:
            raise DomainModelError("A sector range must have positive length.")

    @property
    def sectors(self):
        return self.end_lba - self.start_lba

    def with_end_padding(self, sectors=1):
        if sectors < 0:
            raise DomainModelError("Endpoint padding cannot be negative.")
        return SectorRange(self.start_lba, self.end_lba + sectors)


@dataclass(frozen=True)
class TrackSpan:
    """The exact half-open logical range selected from one track."""

    track: object
    logical_range: SectorRange

    def __post_init__(self):
        if (
            self.logical_range.start_lba < self.track.begin_lba
            or self.logical_range.end_lba > self.track.end_lba
        ):
            raise DomainModelError(
                f"Selected span for Track {self.track.number} lies outside the track."
            )

    @property
    def number(self):
        return self.track.number

    @property
    def kind(self):
        return self.track.kind

    @property
    def begin_lba(self):
        return self.logical_range.start_lba

    @property
    def end_lba(self):
        return self.logical_range.end_lba

    @property
    def length_sectors(self):
        return self.logical_range.sectors

    @property
    def start_offset_sectors(self):
        return self.begin_lba - self.track.begin_lba

    @property
    def is_full_track(self):
        return (
            self.begin_lba == self.track.begin_lba
            and self.end_lba == self.track.end_lba
        )


@dataclass(frozen=True)
class ResolvedSelection:
    tracks: tuple
    spans: tuple = ()

    def __post_init__(self):
        if not self.tracks:
            raise DomainModelError("A resolved selection cannot be empty.")
        if not self.spans:
            object.__setattr__(
                self,
                "spans",
                tuple(
                    TrackSpan(track, SectorRange(track.begin_lba, track.end_lba))
                    for track in self.tracks
                ),
            )
        if tuple(span.track for span in self.spans) != self.tracks:
            raise DomainModelError("Resolved tracks and selected spans must agree.")

    @property
    def first_track(self):
        return self.tracks[0]

    @property
    def last_track(self):
        return self.tracks[-1]

    @property
    def output_sectors(self):
        return sum(span.length_sectors for span in self.spans)


@dataclass(frozen=True)
class ExtractionRequest:
    device: str
    selection: TrackSelection
    include_data: bool
    single_file: bool
    output: object
    prefix: str
    retries: int
    refine_passes: int
    abort_on_skip: bool
    accuraterip: bool
    existing_dump: object = None


@dataclass(frozen=True)
class ExtractionPlan:
    """Complete immutable plan for one bounded extraction."""

    disc: DiscLayout
    selection: ResolvedSelection
    workdir: Path
    image_name: str
    logical_range: SectorRange
    physical_range: SectorRange
    outputs: tuple
    dump_command: list
    refine_command: list
    split_command: list

    def __post_init__(self):
        if self.physical_range.start_lba != self.logical_range.start_lba:
            raise DomainModelError("Physical and logical ranges must start together.")
        if self.physical_range.end_lba != self.logical_range.end_lba + 1:
            raise DomainModelError("Physical range must contain one endpoint sector.")

    @property
    def track_label(self):
        first = self.selection.first_track.number
        last = self.selection.last_track.number
        return f"{first:02d}" if first == last else f"{first:02d}-{last:02d}"

    @property
    def logical_start_lba(self):
        return self.logical_range.start_lba

    @property
    def logical_end_lba(self):
        return self.logical_range.end_lba

    @property
    def dump_start_lba(self):
        return self.physical_range.start_lba

    @property
    def dump_end_lba(self):
        return self.physical_range.end_lba

    @property
    def expected_sectors(self):
        return self.selection.output_sectors


@dataclass(frozen=True)
class AcquisitionResult:
    media_errors: MediaErrors
    refine_passes_used: int

    @property
    def errors(self):
        return {
            "SCSI": self.media_errors.scsi,
            "C2": self.media_errors.c2,
            "Q": self.media_errors.q,
        }


@dataclass(frozen=True)
class ExtractionResult:
    plan: ExtractionPlan
    acquisition: AcquisitionResult
    outputs: tuple
    verification: object
    omitted: tuple = ()
