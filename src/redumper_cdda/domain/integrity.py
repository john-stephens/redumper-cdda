"""Immutable media-integrity domain models."""

from dataclasses import dataclass

from .errors import DomainModelError


@dataclass(frozen=True)
class MediaErrors:
    scsi: int
    c2: int
    q: object

    def __post_init__(self):
        values = (
            (self.scsi, self.c2)
            if self.q is None
            else (self.scsi, self.c2, self.q)
        )
        if min(values) < 0:
            raise DomainModelError("Media-error counts cannot be negative.")

    @property
    def has_data_errors(self):
        return self.scsi != 0 or self.c2 != 0


@dataclass(frozen=True)
class TrackMediaErrors:
    scsi_samples: int
    c2_samples: int
    scsi_sectors: int
    c2_sectors: int

    @property
    def has_data_errors(self):
        return self.scsi_samples != 0 or self.c2_samples != 0


@dataclass(frozen=True)
class WriteOffsetMap:
    boundaries: tuple

    def __post_init__(self):
        if not self.boundaries:
            raise DomainModelError("A write-offset map cannot be empty.")
        if tuple(sorted(self.boundaries)) != self.boundaries:
            raise DomainModelError("Write-offset boundaries must be sorted.")

    def offset_for_lba(self, lba):
        offset = self.boundaries[0][1]
        for boundary_lba, candidate in self.boundaries:
            if boundary_lba > lba:
                break
            offset = candidate
        return offset
