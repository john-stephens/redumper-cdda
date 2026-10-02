"""Redumper command construction adapter."""

import re
from dataclasses import dataclass
from pathlib import Path

from ..domain.errors import IntegrityStatusError
from ..domain.events import LifecycleEvent
from ..domain.extraction import SectorRange
from ..domain.integrity import MediaErrors, TrackMediaErrors, WriteOffsetMap
from ..ports.process import CommandResult


@dataclass(frozen=True)
class RedumperCommandFactory:
    device: str
    workdir: Path
    image_name: str
    retries: int

    def _bounded_command(self, operation, sector_range):
        return [
            "redumper",
            operation,
            f"--drive={self.device}",
            f"--image-path={self.workdir}",
            f"--image-name={self.image_name}",
            f"--retries={self.retries}",
            f"--lba-start={sector_range.start_lba}",
            f"--lba-end={sector_range.end_lba}",
        ]

    def dump(self, sector_range):
        return self._bounded_command("dump", sector_range)

    def refine(self, sector_range):
        return self._bounded_command("refine", sector_range)

    def split(self, include_data=False):
        command = [
            "redumper",
            "split",
            f"--image-path={self.workdir}",
            f"--image-name={self.image_name}",
            "--force-split",
        ]
        if include_data:
            command.append("--filesystem-trim")
        return command


class RedumperClient:
    """Execute the domain operations represented by an extraction plan."""

    _PARTIAL_DATA_TRACK_BASE_LBA_ERROR = "unable to establish base LBA"

    def __init__(self, executor):
        self._executor = executor

    def dump(self, plan, **options):
        return self._run(
            plan.dump_command, progress_tracks=plan.selection.tracks, **options
        )

    def refine(self, plan, **options):
        return self._run(
            plan.refine_command, progress_tracks=plan.selection.tracks, **options
        )

    def split(self, plan, **options):
        result = self._run(plan.split_command, **options)
        if (
            result.returncode != 0
            and self._PARTIAL_DATA_TRACK_BASE_LBA_ERROR in result.output
            and "--force-qtoc" not in plan.split_command
        ):
            # Some redumper builds probe every data track from the stored full
            # TOC during split.  For a partial image ending before a later data
            # track, that probe seeks beyond the image and aborts before any
            # selected audio can be split.  QTOC mode restricts splitting to
            # the tracks actually represented by the partial subchannel dump.
            return self._run([*plan.split_command, "--force-qtoc"], **options)
        return result

    def _run(self, command, **options):
        result = self._executor.run(command, **options)
        return CommandResult(tuple(command), result.returncode, result.output)


class RedumperProcessExecutor:
    """Run redumper while translating its output into reporting events."""

    def __init__(self, runner, reporter):
        self._runner = runner
        self._reporter = reporter

    def run(self, command, progress_tracks=()):
        operation = command[1]
        self._reporter.publish(LifecycleEvent("command_started", tuple(command)))
        last_progress = None
        current_track = None

        def observe(line):
            nonlocal current_track, last_progress
            self._reporter.publish(LifecycleEvent("tool_output", line))
            if operation == "split":
                return
            matches = re.findall(r"\[\s*(\d+)%\]", line)
            if not matches:
                return
            percent = int(matches[-1])
            lba_match = re.search(r"\bLBA\s*:\s*(-?\d+)", line, re.IGNORECASE)
            if lba_match:
                current_track = self._track_for_lba(
                    progress_tracks, int(lba_match.group(1))
                )
            progress = (
                percent, current_track.number if current_track else None
            )
            if progress == last_progress:
                return
            last_progress = progress
            label = "Reading" if operation == "dump" else "Refining"
            if current_track is not None:
                label += (
                    f" data track {current_track.number:02d}"
                    if current_track.kind.value == "data"
                    else f" track {current_track.number:02d}"
                )
            self._reporter.publish(
                LifecycleEvent("progress", {"label": label, "percent": percent})
            )

        result = self._runner.run_streaming(command, observer=observe)
        if last_progress is not None:
            self._reporter.publish(LifecycleEvent("progress_end"))
        return result

    @staticmethod
    def _track_for_lba(tracks, lba):
        for track in tracks:
            if track.begin_lba <= lba < track.end_lba:
                return track
        if tracks and lba < tracks[0].begin_lba:
            return tracks[0]
        if tracks:
            return tracks[-1]
        return None


class RedumperIntegrityParser:
    def __init__(self, media_error_parser, write_offset_parser):
        self._media_error_parser = media_error_parser
        self._write_offset_parser = write_offset_parser

    def media_errors(self, output):
        parsed = self._media_error_parser(output)
        if parsed is None:
            raise IntegrityStatusError(
                "Could not determine redumper SCSI/C2 error status."
            )
        return MediaErrors(parsed["SCSI"], parsed["C2"], parsed["Q"])

    def write_offsets(self, output):
        try:
            return WriteOffsetMap(tuple(self._write_offset_parser(output)))
        except RuntimeError as exc:
            raise IntegrityStatusError(str(exc)) from exc


class RedumperStateInspector:
    def __init__(self, inspector):
        self._inspector = inspector

    def inspect(self, state_path, tracks, offsets):
        try:
            parsed = self._inspector(
                state_path,
                [self._serialize_track(track) for track in tracks],
                list(offsets.boundaries),
            )
        except RuntimeError as exc:
            raise IntegrityStatusError(str(exc)) from exc
        return {
            number: TrackMediaErrors(
                values["SCSI"],
                values["C2"],
                values["SCSI sectors"],
                values["C2 sectors"],
            )
            for number, values in parsed.items()
        }

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
