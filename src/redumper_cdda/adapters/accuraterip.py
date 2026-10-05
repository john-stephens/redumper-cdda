"""AccurateRip verification adapters."""

from ..domain.disc import TrackKind
from ..domain.errors import VerificationError
from ..domain.outputs import VerificationReport
from ..ports.verification import Verifier


class AccurateRipVerifier(Verifier):
    enabled = True

    def __init__(self, verifier):
        self._verifier = verifier

    def prepare(self, plan):
        if not any(
            track.kind is TrackKind.AUDIO and track.number != 0
            for track in plan.selection.tracks
        ):
            self.enabled = False
            return
        disc = self._serialize_layout(plan.disc)
        from ..accuraterip import accuraterip_layout_type

        try:
            accuraterip_layout_type(disc)
        except RuntimeError as exc:
            raise VerificationError(str(exc)) from exc

    def verify(self, plan, split_result, workdir):
        verification = [
            {
                "track": self._serialize_track(item.track),
                "segments": [self._segment_to_legacy(segment) for segment in item.audio_segments],
                "write_offset": item.write_offset,
            }
            for item in split_result.verification_tracks
        ]
        try:
            report = self._verifier(
                self._serialize_layout(plan.disc), verification, workdir
            )
        except RuntimeError as exc:
            raise VerificationError(str(exc)) from exc
        return VerificationReport(report["disc_id"], tuple(report["results"]))

    @staticmethod
    def _segment_to_legacy(segment):
        return {
            "path": segment.path,
            "track": segment.track_number,
            "start_sector": segment.start_sector,
            "sectors": segment.sectors,
            "bin_sectors": segment.bin_sectors,
        }

    @classmethod
    def _serialize_layout(cls, layout):
        return [cls._serialize_track(track) for track in layout.tracks]

    @staticmethod
    def _serialize_track(track):
        return {
            "number": track.number,
            "kind": track.kind.value,
            "control": track.control,
            "length": track.length_sectors,
            "length_msf": track.length_msf,
            "begin": track.begin_lba,
            "begin_msf": track.begin_msf,
            "end": track.end_lba,
        }


class NullVerifier(Verifier):
    enabled = False

    def prepare(self, plan):
        return None

    def verify(self, plan, split_result, workdir):
        return None
