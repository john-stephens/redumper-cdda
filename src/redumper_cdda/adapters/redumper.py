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
        toc_path = plan.workdir / f"{plan.image_name}.toc"
        if (
            plan.logical_end_lba >= plan.disc.lead_out_lba
            or not toc_path.is_file()
        ):
            return self._run(plan.split_command, **options)

        fulltoc_path = plan.workdir / f"{plan.image_name}.fulltoc"
        original_toc = toc_path.read_bytes()
        original_fulltoc = (
            fulltoc_path.read_bytes() if fulltoc_path.is_file() else None
        )
        bounded_toc = self._bounded_toc(plan, original_toc)
        toc_path.write_bytes(bounded_toc)
        if original_fulltoc is not None:
            fulltoc_path.unlink()
        try:
            return self._run(plan.split_command, **options)
        finally:
            toc_path.write_bytes(original_toc)
            if original_fulltoc is not None:
                fulltoc_path.write_bytes(original_fulltoc)

    @staticmethod
    def _bounded_toc(plan, toc):
        if len(toc) < 4:
            return toc
        response_length = int.from_bytes(toc[0:2], "big") + 2
        if response_length > len(toc) or (response_length - 4) % 8:
            return toc
        descriptors = [
            toc[offset:offset + 8]
            for offset in range(4, response_length, 8)
        ]
        selected = {
            track.number
            for track in plan.disc.tracks
            if (
                track.begin_lba < plan.logical_end_lba
                and track.end_lba > plan.logical_start_lba
            )
        }
        if plan.selection.first_track.number == 0:
            selected.add(1)
        track_descriptors = [
            descriptor
            for descriptor in descriptors
            if descriptor[2] in selected
        ]
        leadout = next(
            (bytearray(item) for item in descriptors if item[2] == 0xAA),
            None,
        )
        if not track_descriptors or leadout is None:
            return toc
        leadout[4:8] = plan.logical_end_lba.to_bytes(4, "big")
        bounded_descriptors = b"".join([*track_descriptors, bytes(leadout)])
        data_length = 2 + len(bounded_descriptors)
        return (
            data_length.to_bytes(2, "big")
            + bytes((min(selected), max(selected)))
            + bounded_descriptors
        )

    def _run(self, command, **options):
        result = self._executor.run(command, **options)
        return CommandResult(tuple(command), result.returncode, result.output)


class RedumperProcessExecutor:
    """Run redumper while translating its output into reporting events."""

    _ERROR_COUNTS = re.compile(
        r"errors:\s*\{\s*SCSIs?:\s*(\d+)\s*,\s*"
        r"C2s?:\s*(\d+)\s*,\s*Q:\s*(\d+)\s*\}",
        re.IGNORECASE,
    )

    def __init__(self, runner, reporter):
        self._runner = runner
        self._reporter = reporter
        self._track_errors = {}
        self._total_errors = (0, 0, 0)

    def run(self, command, progress_tracks=()):
        operation = command[1]
        if operation == "dump":
            self._track_errors = {
                track.number: [0, 0, 0] for track in progress_tracks
            }
            self._total_errors = (0, 0, 0)
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
            lba_match = re.search(r"\bLBA\s*:\s*(-?\d+)", line, re.IGNORECASE)
            if lba_match:
                next_track = self._track_for_lba(
                    progress_tracks, int(lba_match.group(1))
                )
                if current_track is not None and next_track is not current_track:
                    self._publish_progress(operation, current_track, 100)
                    self._reporter.publish(LifecycleEvent("progress_end"))
                    last_progress = None
                current_track = next_track
            counts_match = self._ERROR_COUNTS.search(line)
            if counts_match:
                totals = tuple(int(value) for value in counts_match.groups())
                if current_track is not None:
                    track_errors = self._track_errors.setdefault(
                        current_track.number, [0, 0, 0]
                    )
                    for index, (total, previous) in enumerate(
                        zip(totals, self._total_errors)
                    ):
                        track_errors[index] = max(
                            0, track_errors[index] + total - previous
                        )
                self._total_errors = totals
            percent = (
                self._track_percent(current_track, int(lba_match.group(1)))
                if current_track is not None and lba_match
                else int(matches[-1])
            )
            errors = tuple(
                self._track_errors.get(current_track.number, (0, 0, 0))
                if current_track is not None
                else self._total_errors
            )
            progress = (percent, current_track.number if current_track else None, errors)
            if progress == last_progress:
                return
            last_progress = progress
            self._publish_progress(operation, current_track, percent)

        result = self._runner.run_streaming(command, observer=observe)
        if last_progress is not None:
            if current_track is not None and last_progress[0] != 100:
                self._publish_progress(operation, current_track, 100)
            self._reporter.publish(LifecycleEvent("progress_end"))
        return result

    def _publish_progress(self, operation, track, percent):
        label = "Reading" if operation == "dump" else "Refining"
        errors = self._total_errors
        if track is not None:
            label += (
                f" data track {track.number:02d}"
                if track.kind.value == "data"
                else f" track {track.number:02d}"
            )
            errors = self._track_errors.get(track.number, (0, 0, 0))
        self._reporter.publish(
            LifecycleEvent(
                "progress",
                {
                    "label": label,
                    "percent": percent,
                    "scsi": errors[0],
                    "c2": errors[1],
                    "q": errors[2],
                },
            )
        )

    @staticmethod
    def _track_percent(track, lba):
        completed = min(max(lba - track.begin_lba + 1, 0), track.length_sectors)
        return completed * 100 // track.length_sectors

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
