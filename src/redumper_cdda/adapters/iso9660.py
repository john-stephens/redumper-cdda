"""Validated ISO output adapter."""

from ..domain.errors import IsoOutputError


class IsoOutputWriter:
    def __init__(self, converter):
        self._converter = converter

    def write(self, resolved, temporary_path, verbose=False):
        try:
            self._converter(
                self._serialize(resolved),
                temporary_path,
                verbose=verbose,
            )
        except RuntimeError as exc:
            raise IsoOutputError(str(exc)) from exc

    @staticmethod
    def _serialize(resolved):
        source = resolved.data_source
        return {
            "cue_path": source.cue_path,
            "path": source.path,
            "track": source.track_number,
            "track_type": source.track_type,
            "sector_size": source.sector_size,
            "start_sector": source.start_sector,
            "sectors": source.sectors,
            "track_begin_lba": resolved.plan.track.begin_lba,
            "track_sectors": resolved.plan.expected_sectors,
        }
