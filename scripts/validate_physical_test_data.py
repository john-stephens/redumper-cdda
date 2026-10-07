#!/usr/bin/env python3
"""Validate redumper-cdda offline against captured physical-test dumps."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from redumper_cdda.integrity import (  # noqa: E402
    REDUMPER_ERROR_C2,
    REDUMPER_ERROR_SKIP,
    REDUMPER_LBA_START,
    SAMPLES_PER_SECTOR,
    inspect_track_media_errors,
    parse_split_write_offsets,
    state_offset_for_lba,
)


DEFAULT_TEST_DATA = REPOSITORY / "test_data"
DEFAULT_LAUNCHER = REPOSITORY / "redumper-cdda"
PROJECT_PYTHON = REPOSITORY / ".venv" / "bin" / "python"
PCM_BYTES_PER_SECTOR = 2352
PCM_FRAMES_PER_SECTOR = 588
PHYSICAL_PROFILES = (
    "regular-audio",
    "track0-pregap",
    "data-first",
    "data-last",
    "data-only",
)


class ValidationError(RuntimeError):
    """Captured data or an offline extraction failed validation."""


@dataclass(frozen=True)
class ParityOutput:
    path: Path
    kind: str
    sectors: int
    digest: str
    write_offset: int = 0


class ValidationLog:
    def __init__(self, stream):
        self._stream = stream

    def start_test(self, name):
        separator = "=" * 80
        self._stream.write(f"\n{separator}\nTEST: {name}\n{separator}\n")
        self._stream.flush()

    def finish_test(self, name, result):
        separator = "-" * 80
        self._stream.write(
            f"\n{separator}\nTEST RESULT: {result} — {name}\n{separator}\n"
        )
        self._stream.flush()

    def record(self, command, cwd, result):
        self._stream.write(f"\nWorking directory: {cwd}\n")
        self._stream.write("Command: " + " ".join(str(item) for item in command) + "\n")
        self._stream.write(f"Exit status: {result.returncode}\n")
        if result.stdout:
            self._stream.write("\nCaptured output\n---------------\n")
            self._stream.write(result.stdout)
            if not result.stdout.endswith("\n"):
                self._stream.write("\n")
        detail_path = next(
            (
                Path(argument.split("=", 1)[1])
                for argument in command
                if str(argument).startswith("--log-file=")
            ),
            None,
        )
        if detail_path is not None and detail_path.is_file():
            self._stream.write("\nVerbose application log\n-----------------------\n")
            self._stream.write(
                detail_path.read_text(encoding="utf-8", errors="replace")
            )
            self._stream.write("\n")
        self._stream.flush()

    def record_skip(self, reason):
        self._stream.write(f"\nSkip reason: {reason}\n")
        self._stream.flush()


@contextmanager
def validation_test(validation_log, name):
    print(f"{name} ... ", end="", flush=True)
    if validation_log is None:
        try:
            yield
        except BaseException:
            print("FAIL", flush=True)
            raise
        else:
            print("PASS", flush=True)
        return
    validation_log.start_test(name)
    try:
        yield
    except BaseException:
        validation_log.finish_test(name, "FAIL")
        print("FAIL", flush=True)
        raise
    else:
        validation_log.finish_test(name, "PASS")
        print("PASS", flush=True)


def skipped_test(validation_log, name, reason):
    print(f"{name} ... SKIP ({reason})", flush=True)
    if validation_log is not None:
        validation_log.start_test(name)
        validation_log.record_skip(reason)
        validation_log.finish_test(name, "SKIP")


def parser():
    result = argparse.ArgumentParser(
        description=(
            "Run media-free extraction and integrity checks against captured "
            "redumper test data."
        )
    )
    result.add_argument(
        "--test-data",
        type=Path,
        default=DEFAULT_TEST_DATA,
        help="captured data root (default: repository test_data directory)",
    )
    result.add_argument(
        "--profile",
        action="append",
        help="validate only this profile; repeat to select multiple profiles",
    )
    result.add_argument(
        "--launcher",
        type=Path,
        default=DEFAULT_LAUNCHER,
        help="redumper-cdda launcher to test",
    )
    result.add_argument(
        "--keep-work",
        type=Path,
        metavar="PATH",
        help="retain generated outputs in a new directory instead of using temporary storage",
    )
    result.add_argument(
        "--skip-source-hashes",
        action="store_true",
        help="skip the before/after SHA256SUMS checks",
    )
    result.add_argument(
        "--log-file",
        type=Path,
        metavar="PATH",
        help="write consolidated commands and verbose extraction output to PATH",
    )
    return result


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_entries(profile_dir):
    checksum_path = profile_dir / "SHA256SUMS"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValidationError(f"could not read {checksum_path}: {exc}") from exc
    entries = {}
    for line_number, line in enumerate(lines, 1):
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if not match:
            raise ValidationError(
                f"malformed checksum at {checksum_path}:{line_number}"
            )
        relative = Path(match.group(2))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError(f"unsafe checksum path: {relative}")
        if relative in entries:
            raise ValidationError(f"duplicate checksum path: {relative}")
        entries[relative] = match.group(1).lower()
    return entries


def verify_checksums(profile_dir):
    entries = checksum_entries(profile_dir)
    actual_paths = {
        path.relative_to(profile_dir)
        for path in profile_dir.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    expected_paths = set(entries)
    if actual_paths != expected_paths:
        missing = sorted(str(path) for path in expected_paths - actual_paths)
        unlisted = sorted(str(path) for path in actual_paths - expected_paths)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unlisted:
            details.append("unlisted: " + ", ".join(unlisted))
        raise ValidationError(
            f"checksum file list differs for {profile_dir.name} ({'; '.join(details)})"
        )
    for relative, expected in entries.items():
        actual = sha256_file(profile_dir / relative)
        if actual != expected:
            raise ValidationError(
                f"checksum mismatch: {profile_dir.name}/{relative}"
            )
    return entries


def load_manifests(test_data, requested=()):
    try:
        paths = sorted(test_data.glob("*/manifest.json"))
    except OSError as exc:
        raise ValidationError(f"could not inspect {test_data}: {exc}") from exc
    manifests = []
    requested = set(requested or ())
    for path in paths:
        if requested and path.parent.name not in requested:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"could not read {path}: {exc}") from exc
        if manifest.get("format") != 1 or manifest.get("status") != "complete":
            raise ValidationError(f"capture manifest is not complete: {path}")
        if manifest.get("profile") != path.parent.name:
            raise ValidationError(f"profile name disagrees with directory: {path}")
        scenarios = manifest.get("scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            raise ValidationError(f"manifest contains no scenarios: {path}")
        if any(item.get("status") != "complete" for item in scenarios):
            raise ValidationError(f"manifest contains an incomplete scenario: {path}")
        manifests.append((path.parent, manifest))
    return manifests


def safe_child(root, relative, label):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"unsafe {label} path: {relative}")
    result = (root / relative).resolve()
    try:
        result.relative_to(root.resolve())
    except ValueError as exc:
        raise ValidationError(f"unsafe {label} path: {relative}") from exc
    return result


def selected_track(manifest, number):
    if number == 0:
        track_one = next(
            (item for item in manifest["layout"]["tracks"] if item["number"] == 1),
            None,
        )
        if track_one is None or track_one["kind"] != "audio":
            raise ValidationError("Track 0 capture has no audio Track 1")
        return {
            "number": 0,
            "kind": "audio",
            "begin_lba": 0,
            "length_sectors": track_one["begin_lba"],
        }
    track = next(
        (item for item in manifest["layout"]["tracks"] if item["number"] == number),
        None,
    )
    if track is None:
        raise ValidationError(f"manifest layout has no Track {number}")
    return track


def selection_text(numbers):
    if not numbers:
        raise ValidationError("scenario has no selected tracks")
    expected = list(range(numbers[0], numbers[-1] + 1))
    if numbers != expected:
        raise ValidationError(f"scenario tracks are not contiguous: {numbers}")
    return str(numbers[0]) if len(numbers) == 1 else f"{numbers[0]}-{numbers[-1]}"


def extraction_command(
    launcher,
    profile_dir,
    scenario,
    output_dir,
    *,
    single_file=False,
    abort_on_skip=False,
    show_layout=False,
    accuraterip=False,
):
    prefix = safe_child(
        profile_dir, scenario["existing_dump"], "existing dump"
    )
    toc = safe_child(
        profile_dir, scenario["cdparanoia_toc_file"], "cdparanoia TOC"
    )
    command = launcher_command(launcher)
    if show_layout:
        command.append("--show-layout")
    else:
        command.append(selection_text(scenario["selected_tracks"]))
    command.extend(
        [
            f"--existing-dump={prefix}",
            f"--cdparanoia-toc-file={toc}",
        ]
    )
    if not accuraterip:
        command.append("--no-accuraterip")
    if scenario.get("include_data"):
        command.append("--include-data")
    if single_file:
        command.append("--single-file")
    if abort_on_skip:
        command.append("--abort-on-skip")
    if not show_layout:
        command.append(f"--log-file={output_dir / 'validation.log'}")
    return command


def launcher_command(launcher):
    launcher = launcher.resolve()
    if launcher == DEFAULT_LAUNCHER.resolve():
        python = PROJECT_PYTHON if PROJECT_PYTHON.is_file() else Path(sys.executable)
        return [str(python), str(launcher)]
    return [str(launcher)]


def run_command(command, cwd, expect_success, validation_log=None):
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if validation_log is not None:
        validation_log.record(command, cwd, result)
    if (result.returncode == 0) != expect_success:
        tail = "\n".join(result.stdout.splitlines()[-30:])
        detail = next(
            (
                Path(str(item).removeprefix("--log-file="))
                for item in command
                if str(item).startswith("--log-file=")
            ),
            None,
        )
        if detail is not None and detail.is_file():
            detail_tail = "\n".join(
                detail.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
            )
            if detail_tail and detail_tail not in tail:
                tail = f"{tail}\n\nExtraction log tail:\n{detail_tail}"
        expectation = "success" if expect_success else "failure"
        raise ValidationError(
            f"expected {expectation}, got status {result.returncode}: "
            f"{' '.join(str(item) for item in command)}\n{tail}"
        )
    return result


def assert_offline_log(log_path):
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValidationError(f"could not read extraction log {log_path}: {exc}") from exc
    if re.search(r"^\+ redumper (?:dump|refine)\b", output, re.MULTILINE):
        raise ValidationError(f"offline extraction attempted acquisition: {log_path}")
    splits = re.findall(r"^\+ redumper split\b", output, re.MULTILINE)
    if len(splits) != 1:
        raise ValidationError(
            f"offline extraction expected one split, found {len(splits)}: {log_path}"
        )


def accuraterip_tracks(manifest, scenario):
    disc_tracks = manifest["layout"]["tracks"]
    if disc_tracks and disc_tracks[0]["kind"] == "data":
        return ()
    return tuple(
        number
        for number in scenario["selected_tracks"]
        if number != 0 and selected_track(manifest, number)["kind"] == "audio"
    )


def expected_accuraterip_results(profile_dir, manifest, scenario, log_path):
    numbers = accuraterip_tracks(manifest, scenario)
    if not numbers:
        return {}
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace")
        offsets = parse_split_write_offsets(output)
        tracks = [
            {
                "number": number,
                "begin": selected_track(manifest, number)["begin_lba"],
                "end": (
                    selected_track(manifest, number)["begin_lba"]
                    + selected_track(manifest, number)["length_sectors"]
                ),
            }
            for number in numbers
        ]
        prefix = safe_child(
            profile_dir, scenario["existing_dump"], "existing dump"
        )
        errors = inspect_track_media_errors(
            prefix.with_suffix(".state"), tracks, offsets
        )
    except (OSError, RuntimeError) as exc:
        raise ValidationError(
            f"could not determine AccurateRip expectations from captured state: {exc}"
        ) from exc
    return {
        number: (
            "no match"
            if errors[number]["SCSI"] or errors[number]["C2"]
            else "verified"
        )
        for number in numbers
    }


def assert_accuraterip(log_path, expected_results):
    if not expected_results:
        return
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValidationError(f"could not read extraction log {log_path}: {exc}") from exc
    results = {
        int(number): status
        for number, status in re.findall(
            r"^Track (\d+): (verified|not present in the database|no match)\b",
            output,
            re.MULTILINE,
        )
    }
    if set(results) != set(expected_results):
        raise ValidationError(
            f"AccurateRip results differ in {log_path}: "
            f"expected Tracks {sorted(expected_results)}, got {sorted(results)}"
        )
    mismatches = [
        (number, expected_results[number], results[number])
        for number in sorted(expected_results)
        if results[number] != expected_results[number]
    ]
    if mismatches:
        raise ValidationError(
            "AccurateRip result mismatch: "
            + ", ".join(
                f"Track {number:02d} expected {expected}, got {actual}"
                for number, expected, actual in mismatches
            )
        )


def assert_accuraterip_track(log_path, number, expected_status):
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValidationError(f"could not read extraction log {log_path}: {exc}") from exc
    match = re.search(
        rf"^Track {number:02d}: (verified|not present in the database|no match)\b",
        output,
        re.MULTILINE,
    )
    if match is None or match.group(1) != expected_status:
        actual = match.group(1) if match is not None else "missing"
        raise ValidationError(
            f"Track {number:02d} expected {expected_status}, got {actual}"
        )


def assert_required_write_offset(manifest, scenario, log_path):
    required_sign = scenario.get("required_write_offset_sign", 0)
    if not required_sign:
        return
    number = scenario["strict_accuraterip_track"]
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace")
        offsets = parse_split_write_offsets(output)
        offset = state_offset_for_lba(
            offsets, selected_track(manifest, number)["begin_lba"]
        )
    except (OSError, RuntimeError) as exc:
        raise ValidationError(
            f"could not determine required write offset from {log_path}: {exc}"
        ) from exc
    if offset == 0 or (offset > 0) != (required_sign > 0):
        direction = "positive" if required_sign > 0 else "negative"
        raise ValidationError(
            f"Track {number:02d} requires a {direction} nonzero write offset; "
            f"split reported {offset:+d}"
        )


def wave_pcm_hash(path, expected_sectors):
    digest = hashlib.sha256()
    try:
        with wave.open(str(path), "rb") as stream:
            if (
                stream.getnchannels() != 2
                or stream.getsampwidth() != 2
                or stream.getframerate() != 44100
                or stream.getcomptype() != "NONE"
            ):
                raise ValidationError(f"unsupported WAV format: {path}")
            expected_frames = expected_sectors * PCM_FRAMES_PER_SECTOR
            if stream.getnframes() != expected_frames:
                raise ValidationError(
                    f"wrong WAV frame count for {path}: "
                    f"{stream.getnframes()} != {expected_frames}"
                )
            total = 0
            while True:
                data = stream.readframes(PCM_FRAMES_PER_SECTOR * 1024)
                if not data:
                    break
                total += len(data)
                digest.update(data)
    except (OSError, EOFError, wave.Error) as exc:
        raise ValidationError(f"could not validate WAV {path}: {exc}") from exc
    expected_bytes = expected_sectors * PCM_BYTES_PER_SECTOR
    if total != expected_bytes:
        raise ValidationError(
            f"wrong PCM byte count for {path}: {total} != {expected_bytes}"
        )
    return digest.hexdigest()


def iso_hash(path, expected_track_sectors):
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(16 * 2048)
            descriptor = stream.read(2048)
    except OSError as exc:
        raise ValidationError(f"could not validate ISO {path}: {exc}") from exc
    if len(descriptor) != 2048 or descriptor[0:7] != b"\x01CD001\x01":
        raise ValidationError(f"missing ISO9660 primary volume descriptor: {path}")
    little_blocks = int.from_bytes(descriptor[80:84], "little")
    big_blocks = int.from_bytes(descriptor[84:88], "big")
    little_block_size = int.from_bytes(descriptor[128:130], "little")
    big_block_size = int.from_bytes(descriptor[130:132], "big")
    if little_blocks != big_blocks or little_block_size != big_block_size:
        raise ValidationError(f"ISO9660 endian fields disagree: {path}")
    if little_block_size != 2048 or size != little_blocks * 2048:
        raise ValidationError(f"ISO9660 output is not exactly trimmed: {path}")
    if little_blocks <= 0 or little_blocks > expected_track_sectors:
        raise ValidationError(
            f"ISO9660 volume exceeds its data track in {path}: "
            f"{little_blocks} > {expected_track_sectors} sectors"
        )

    path_table_size = int.from_bytes(descriptor[132:136], "little")
    if path_table_size != int.from_bytes(descriptor[136:140], "big"):
        raise ValidationError(f"ISO9660 path-table sizes disagree: {path}")
    for offset, byteorder in (
        (140, "little"),
        (144, "little"),
        (148, "big"),
        (152, "big"),
    ):
        location = int.from_bytes(descriptor[offset:offset + 4], byteorder)
        if location and location * 2048 + path_table_size > size:
            raise ValidationError(
                f"ISO9660 path table lies outside the output: {path}"
            )

    root = descriptor[156:190]
    if root[0] < 34:
        raise ValidationError(f"ISO9660 root directory record is malformed: {path}")
    root_extent = int.from_bytes(root[2:6], "little")
    root_size = int.from_bytes(root[10:14], "little")
    if (
        root_extent != int.from_bytes(root[6:10], "big")
        or root_size != int.from_bytes(root[14:18], "big")
    ):
        raise ValidationError(f"ISO9660 root directory fields disagree: {path}")
    if root_extent * 2048 + root_size > size:
        raise ValidationError(
            f"ISO9660 root directory lies outside the output: {path}"
        )
    return sha256_file(path)


def expected_outputs(manifest, scenario, directory, single_file=False):
    tracks = [selected_track(manifest, number) for number in scenario["selected_tracks"]]
    if single_file and len(tracks) > 1 and all(
        track["kind"] == "audio" for track in tracks
    ):
        return [(directory / "track.wav", "audio", sum(
            track["length_sectors"] for track in tracks
        ), None)]
    return [
        (
            directory / f"track{track['number']:02d}."
            f"{'wav' if track['kind'] == 'audio' else 'iso'}",
            track["kind"],
            track["length_sectors"],
            track["number"],
        )
        for track in tracks
    ]


def validate_output_set(expected):
    directory = expected[0][0].parent
    actual = set(directory.glob("*.wav")) | set(directory.glob("*.iso"))
    expected_paths = {item[0] for item in expected}
    if actual != expected_paths:
        raise ValidationError(
            f"output set differs in {directory}: "
            f"expected {sorted(path.name for path in expected_paths)}, "
            f"got {sorted(path.name for path in actual)}"
        )
    hashes = {}
    for path, kind, sectors, number in expected:
        hashes[number] = (
            wave_pcm_hash(path, sectors)
            if kind == "audio"
            else iso_hash(path, sectors)
        )
    return hashes


def combined_pcm_hash(paths):
    digest = hashlib.sha256()
    for path in paths:
        try:
            with wave.open(str(path), "rb") as stream:
                while True:
                    data = stream.readframes(PCM_FRAMES_PER_SECTOR * 1024)
                    if not data:
                        break
                    digest.update(data)
        except (OSError, EOFError, wave.Error) as exc:
            raise ValidationError(f"could not compare WAV {path}: {exc}") from exc
    return digest.hexdigest()


def pcm_window_hash(path, start_frame, frames):
    digest = hashlib.sha256()
    try:
        with wave.open(str(path), "rb") as stream:
            stream.setpos(start_frame)
            remaining = frames
            while remaining:
                data = stream.readframes(min(remaining, PCM_FRAMES_PER_SECTOR * 1024))
                if not data:
                    break
                digest.update(data)
                remaining -= len(data) // 4
    except (OSError, EOFError, wave.Error) as exc:
        raise ValidationError(f"could not compare WAV {path}: {exc}") from exc
    if remaining:
        raise ValidationError(f"short PCM comparison window in {path}")
    return digest.hexdigest()


def parity_outputs(manifest, expected, hashes, log_path):
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace")
        offsets = parse_split_write_offsets(log)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(
            f"could not determine split offsets from {log_path}: {exc}"
        ) from exc

    outputs = {}
    for path, kind, sectors, number in expected:
        track = selected_track(manifest, number)
        write_offset = 0
        if kind == "audio":
            begin_lba = track["begin_lba"]
            end_lba = begin_lba + track["length_sectors"]
            write_offset = state_offset_for_lba(offsets, begin_lba)
            for boundary_lba, _candidate in offsets[1:]:
                if begin_lba < boundary_lba < end_lba:
                    raise ValidationError(
                        "cannot compare an audio track whose redumper write "
                        f"offset changes internally: Track {number:02d}"
                    )
        outputs[number] = ParityOutput(
            path=path,
            kind=kind,
            sectors=sectors,
            digest=hashes[number],
            write_offset=write_offset,
        )
    return outputs


def validate_track_parity(profile, number, outputs):
    kinds = {item.kind for item in outputs}
    sector_counts = {item.sectors for item in outputs}
    if len(kinds) != 1 or len(sector_counts) != 1:
        raise ValidationError(
            f"Track {number:02d} has inconsistent metadata across {profile} scenarios"
        )
    if kinds == {"data"}:
        matches = len({item.digest for item in outputs}) == 1
    else:
        frames = outputs[0].sectors * PCM_FRAMES_PER_SECTOR
        common_start = max(item.write_offset for item in outputs)
        common_end = min(item.write_offset + frames for item in outputs)
        if common_start >= common_end:
            raise ValidationError(
                f"Track {number:02d} has no comparable PCM across {profile} scenarios"
            )
        matches = len({
            pcm_window_hash(
                item.path,
                common_start - item.write_offset,
                common_end - common_start,
            )
            for item in outputs
        }) == 1
    if not matches:
        raise ValidationError(
            f"Track {number:02d} differs across {profile} scenarios"
        )


def clean_tracks(manifest):
    return {
        number
        for scenario in manifest["scenarios"]
        if not scenario.get("expected_media_errors", False)
        for number in scenario["selected_tracks"]
    }


def strict_known_clean_tracks(manifest, scenario):
    result = clean_tracks(manifest) & set(scenario["selected_tracks"])
    fabricated = scenario_fabricated_c2_track(manifest, scenario)
    if fabricated is not None:
        result.discard(fabricated)
    return result


def scenario_fabricated_c2_track(manifest, scenario):
    fabricated = scenario.get("fabricated_c2_track")
    if fabricated is not None:
        return fabricated
    if manifest["profile"] == "regular-audio" and scenario["name"] in (
        "a02-track-02",
        "a03-a04-all-audio",
    ):
        return 2
    return None


def layout_probe_scenario(manifest):
    probe = manifest.get("write_offset_probe", {}).get("scenario")
    if probe is not None:
        for scenario in manifest["scenarios"]:
            if scenario["name"] == probe:
                return scenario
    all_tracks = {track["number"] for track in manifest["layout"]["tracks"]}
    for scenario in manifest["scenarios"]:
        if set(scenario["selected_tracks"]) == all_tracks:
            return scenario
    return manifest["scenarios"][0]


def probe_captured_write_offsets(
    profile_dir, scenario, destination, validation_log=None
):
    source_prefix = safe_child(
        profile_dir, scenario["existing_dump"], "existing dump"
    )
    destination.mkdir()
    image_name = "offset-probe"
    copied = 0
    for source in source_prefix.parent.iterdir():
        if source.is_file() and source.name.startswith(f"{source_prefix.name}."):
            shutil.copy2(source, destination / f"{image_name}{source.suffix}")
            copied += 1
    if not copied:
        raise ValidationError(f"no dump files found for {source_prefix}")
    command = [
        "redumper",
        "split",
        f"--image-path={destination}",
        f"--image-name={image_name}",
        "--force-split",
    ]
    if scenario.get("include_data"):
        command.append("--filesystem-trim")
    result = run_command(
        command, destination, expect_success=True, validation_log=validation_log
    )
    try:
        return [offset for _lba, offset in parse_split_write_offsets(result.stdout)]
    except RuntimeError as exc:
        raise ValidationError(
            "could not determine captured automatic write offset"
        ) from exc


def validate_capture_write_offsets(manifest, observed_offsets=None):
    profile = manifest["profile"]
    if profile not in ("regular-audio", "data-last"):
        return
    try:
        offsets = manifest["write_offset_probe"]["offsets"]
        values = [int(item[1]) for item in offsets]
    except (KeyError, TypeError, ValueError, IndexError):
        values = list(observed_offsets or ())
    if not values:
        raise ValidationError(
            f"{profile} capture lacks a valid automatic write-offset probe"
        )
    if profile == "regular-audio" and any(values):
        raise ValidationError(
            f"{profile} capture requires zero write offset; recorded {values}"
        )
    if profile == "regular-audio":
        synthetic = {
            scenario["name"]: scenario_fabricated_c2_track(manifest, scenario)
            for scenario in manifest.get("scenarios", ())
            if scenario["name"] in ("a02-track-02", "a03-a04-all-audio")
        }
        if synthetic != {
            "a02-track-02": 2,
            "a03-a04-all-audio": 2,
        }:
            raise ValidationError(
                "regular-audio capture lacks the merged synthetic error regressions"
            )
    if profile == "data-last" and not any(offset < 0 for offset in values):
        raise ValidationError(
            "data-last capture requires a negative nonzero write offset"
        )
    if profile == "data-last":
        regressions = [
            scenario
            for scenario in manifest["scenarios"]
            if scenario.get("fabricated_c2_track") is not None
        ]
        if len(regressions) != 1:
            raise ValidationError(
                "data-last capture lacks the merged nonzero-offset regression"
            )
        regression = regressions[0]
        target = regression.get("strict_accuraterip_track")
        alignment = regression.get("fabricated_c2_track")
        controls = [
            scenario
            for scenario in manifest["scenarios"]
            if scenario.get("selected_tracks") == [target]
            and not scenario.get("include_data", False)
        ]
        if (
            target is None
            or alignment != target + 1
            or regression.get("omitted_alignment_track") != alignment
            or regression.get("required_write_offset_sign") != -1
            or not controls
        ):
            raise ValidationError(
                "data-last capture lacks the merged nonzero-offset regression"
            )


def parity_candidates(scenario, outputs):
    if not scenario.get("expected_media_errors", False):
        return outputs
    number = scenario.get("strict_accuraterip_track")
    if number is not None and number in outputs:
        return {number: outputs[number]}
    return {}


def record_track_parity(profile, candidates, baselines, baseline_dir=None):
    """Compare candidates immediately, retaining at most one output per track."""
    for number, output in candidates.items():
        key = (profile, number)
        baseline = baselines.get(key)
        if baseline is not None:
            validate_track_parity(profile, number, (baseline, output))
            continue
        if baseline_dir is not None:
            baseline_dir.mkdir(parents=True, exist_ok=True)
            retained = baseline_dir / f"track{number:02d}{output.path.suffix}"
            output.path.replace(retained)
            output = replace(output, path=retained)
        baselines[key] = output


def fabricate_c2_dump(profile_dir, manifest, scenario, destination, split_log):
    source_prefix = safe_child(
        profile_dir, scenario["existing_dump"], "existing dump"
    )
    destination.mkdir()
    dump_dir = destination / "dump"
    dump_dir.mkdir()
    target_prefix = dump_dir / source_prefix.name
    copied = 0
    for source in source_prefix.parent.iterdir():
        if source.is_file() and source.name.startswith(f"{source_prefix.name}."):
            shutil.copy2(source, dump_dir / source.name)
            copied += 1
    if not copied:
        raise ValidationError(f"no dump files found for {source_prefix}")
    toc_source = safe_child(
        profile_dir, scenario["cdparanoia_toc_file"], "cdparanoia TOC"
    )
    toc_target = destination / "cdparanoia-toc.txt"
    shutil.copy2(toc_source, toc_target)

    track_number = scenario["fabricated_c2_track"]
    track = selected_track(manifest, track_number)
    lba = track["begin_lba"] + track["length_sectors"] // 2
    try:
        offsets = parse_split_write_offsets(
            split_log.read_text(encoding="utf-8", errors="replace")
        )
        sample_offset = state_offset_for_lba(offsets, lba)
        file_sample = (
            (lba - REDUMPER_LBA_START) * SAMPLES_PER_SECTOR + sample_offset
        )
        state_path = target_prefix.with_suffix(".state")
        with state_path.open("r+b") as state:
            state.seek(file_sample)
            original = state.read(1)
            if len(original) != 1:
                raise ValidationError("fabricated C2 sample lies outside state file")
            if original[0] in (REDUMPER_ERROR_SKIP, REDUMPER_ERROR_C2):
                raise ValidationError(
                    f"{manifest['profile']} capture is not clean at fabricated "
                    "C2 sample"
                )
            state.seek(file_sample)
            state.write(bytes((REDUMPER_ERROR_C2,)))
    except OSError as exc:
        raise ValidationError(f"could not fabricate C2 state: {exc}") from exc

    record = {
        "kind": "synthetic C2 state; captured PCM is unchanged",
        "track": track_number,
        "lba": lba,
        "state_file_sample": file_sample,
        "original_state": original[0],
        "fabricated_state": REDUMPER_ERROR_C2,
    }
    (destination / "fabricated-c2.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    derived = dict(scenario)
    derived["existing_dump"] = str(target_prefix.relative_to(destination))
    derived["cdparanoia_toc_file"] = toc_target.name
    return destination, derived


def validate_scenario(
    launcher, profile_dir, manifest, scenario, workdir, validation_log=None
):
    scenario_dir = workdir / scenario["name"]
    separate_dir = scenario_dir / "separate"
    separate_dir.mkdir(parents=True)
    accuraterip_numbers = accuraterip_tracks(manifest, scenario)
    command = extraction_command(
        launcher,
        profile_dir,
        scenario,
        separate_dir,
        accuraterip=bool(accuraterip_numbers),
    )
    run_command(command, separate_dir, expect_success=True, validation_log=validation_log)
    assert_offline_log(separate_dir / "validation.log")
    accuraterip_results = expected_accuraterip_results(
        profile_dir, manifest, scenario, separate_dir / "validation.log"
    )
    assert_accuraterip(separate_dir / "validation.log", accuraterip_results)
    expected = expected_outputs(manifest, scenario, separate_dir)
    separate_hashes = validate_output_set(expected)

    fabricated_track = scenario_fabricated_c2_track(manifest, scenario)
    if fabricated_track is not None:
        warned_dir = scenario_dir / "warned"
        warned_dir.mkdir()
        fabricated_profile_dir, fabricated_scenario = fabricate_c2_dump(
            profile_dir,
            manifest,
            {**scenario, "fabricated_c2_track": fabricated_track},
            warned_dir / "fabricated-source",
            separate_dir / "validation.log",
        )
        command = extraction_command(
            launcher,
            fabricated_profile_dir,
            fabricated_scenario,
            warned_dir,
            accuraterip=bool(accuraterip_numbers),
        )
        warned_result = run_command(
            command, warned_dir, expect_success=True, validation_log=validation_log
        )
        warned_log = warned_dir / "validation.log"
        assert_offline_log(warned_log)
        synthetic_accuraterip = dict(accuraterip_results)
        if fabricated_track in synthetic_accuraterip:
            synthetic_accuraterip[fabricated_track] = "no match"
        assert_accuraterip(warned_log, synthetic_accuraterip)
        validate_output_set(
            expected_outputs(manifest, scenario, warned_dir)
        )
        if "writing output from the existing dump with unresolved" not in (
            warned_result.stdout
        ):
            raise ValidationError(
                f"synthetic error did not produce a warning: {scenario['name']}"
            )

    tracks = [selected_track(manifest, number) for number in scenario["selected_tracks"]]
    all_audio = all(track["kind"] == "audio" for track in tracks)
    if len(tracks) > 1 and all_audio:
        single_dir = scenario_dir / "single"
        single_dir.mkdir()
        command = extraction_command(
            launcher,
            profile_dir,
            scenario,
            single_dir,
            single_file=True,
            accuraterip=bool(accuraterip_numbers),
        )
        run_command(command, single_dir, expect_success=True, validation_log=validation_log)
        assert_offline_log(single_dir / "validation.log")
        assert_accuraterip(single_dir / "validation.log", accuraterip_results)
        single_expected = expected_outputs(
            manifest, scenario, single_dir, single_file=True
        )
        combined_hash = validate_output_set(single_expected)[None]
        separate_paths = [item[0] for item in expected]
        if combined_hash != combined_pcm_hash(separate_paths):
            raise ValidationError(
                f"combined PCM differs from separate tracks: {scenario['name']}"
            )
    elif len(tracks) == 1 and tracks[0]["kind"] == "data":
        single_dir = scenario_dir / "single-data"
        single_dir.mkdir()
        command = extraction_command(
            launcher,
            profile_dir,
            scenario,
            single_dir,
            single_file=True,
            accuraterip=bool(accuraterip_numbers),
        )
        run_command(command, single_dir, expect_success=True, validation_log=validation_log)
        assert_offline_log(single_dir / "validation.log")
        assert_accuraterip(single_dir / "validation.log", accuraterip_results)
        single_hashes = validate_output_set(
            expected_outputs(manifest, scenario, single_dir, single_file=True)
        )
        if single_hashes != separate_hashes:
            raise ValidationError(
                f"single-file data differs from separate output: {scenario['name']}"
            )
    elif len(tracks) > 1 and scenario.get("include_data"):
        invalid_dir = scenario_dir / "invalid-single"
        invalid_dir.mkdir()
        command = extraction_command(
            launcher, profile_dir, scenario, invalid_dir, single_file=True
        )
        run_command(command, invalid_dir, expect_success=False, validation_log=validation_log)
        if list(invalid_dir.glob("*.wav")) or list(invalid_dir.glob("*.iso")):
            raise ValidationError(
                f"invalid mixed single-file mode created output: {scenario['name']}"
            )

    if scenario.get("expected_media_errors", False) or fabricated_track is not None:
        strict_dir = scenario_dir / "strict"
        strict_dir.mkdir()
        strict_profile_dir = profile_dir
        strict_scenario = scenario
        if fabricated_track is not None:
            strict_profile_dir, strict_scenario = fabricate_c2_dump(
                profile_dir,
                manifest,
                {**scenario, "fabricated_c2_track": fabricated_track},
                strict_dir / "fabricated-source",
                separate_dir / "validation.log",
            )
        command = extraction_command(
            launcher,
            strict_profile_dir,
            strict_scenario,
            strict_dir,
            abort_on_skip=True,
            accuraterip=bool(scenario.get("strict_accuraterip_track")),
        )
        run_command(command, strict_dir, expect_success=False, validation_log=validation_log)
        actual = set(strict_dir.glob("*.wav")) | set(strict_dir.glob("*.iso"))
        all_paths = {item[0].name for item in expected}
        actual_names = {path.name for path in actual}
        if not actual_names < all_paths:
            raise ValidationError(
                f"strict error policy did not omit affected output: {scenario['name']}"
            )
        known_clean = strict_known_clean_tracks(manifest, scenario)
        clean_names = {
            f"track{number:02d}."
            f"{'wav' if selected_track(manifest, number)['kind'] == 'audio' else 'iso'}"
            for number in known_clean
        }
        if not clean_names <= actual_names:
            raise ValidationError(
                f"strict error policy omitted a known-clean track: {scenario['name']}"
            )
        strict_track = scenario.get("strict_accuraterip_track")
        if strict_track is not None:
            required_name = f"track{strict_track:02d}.wav"
            omitted_track = scenario["omitted_alignment_track"]
            omitted_name = f"track{omitted_track:02d}.wav"
            if required_name not in actual_names or omitted_name in actual_names:
                raise ValidationError(
                    "strict offset fixture did not retain the target while "
                    "omitting its alignment neighbor"
                )
            strict_log = strict_dir / "validation.log"
            assert_required_write_offset(manifest, scenario, strict_log)
            assert_accuraterip_track(strict_log, strict_track, "verified")
        if len(tracks) > 1 and all_audio:
            strict_single_dir = scenario_dir / "strict-single"
            strict_single_dir.mkdir()
            command = extraction_command(
                launcher,
                strict_profile_dir,
                strict_scenario,
                strict_single_dir,
                single_file=True,
                abort_on_skip=True,
            )
            run_command(
                command,
                strict_single_dir,
                expect_success=False,
                validation_log=validation_log,
            )
            if list(strict_single_dir.glob("*.wav")):
                raise ValidationError(
                    f"strict combined mode created output: {scenario['name']}"
                )
    return parity_outputs(
        manifest,
        expected,
        separate_hashes,
        separate_dir / "validation.log",
    )


@contextmanager
def work_directory(keep_work):
    if keep_work is None:
        with tempfile.TemporaryDirectory(prefix="redumper-cdda-validation-") as value:
            yield Path(value)
        return
    path = keep_work.resolve()
    if path.exists():
        raise ValidationError(f"--keep-work path already exists: {path}")
    path.mkdir(parents=True)
    yield path


@contextmanager
def validation_log_file(path):
    if path is None:
        yield None
        return
    path = path.resolve()
    try:
        with path.open("w", encoding="utf-8") as stream:
            yield ValidationLog(stream)
    except OSError as exc:
        raise ValidationError(f"could not write validation log {path}: {exc}") from exc


def validate(args, validation_log=None):
    launcher = args.launcher.resolve()
    if not launcher.is_file() or not launcher.stat().st_mode & 0o111:
        raise ValidationError(f"launcher is not executable: {launcher}")
    manifests = load_manifests(
        args.test_data.resolve(), args.profile or PHYSICAL_PROFILES
    )
    requested = set(args.profile or PHYSICAL_PROFILES)
    found = {manifest["profile"] for _directory, manifest in manifests}
    missing = sorted(requested - found)
    for profile in missing:
        skipped_test(
            validation_log,
            f"[{profile}] captured test cases",
            "no data available",
        )
    if manifests and shutil.which("redumper") is None:
        raise ValidationError("redumper not found; offline splitting still requires it")
    source_hashes = {}
    if not args.skip_source_hashes:
        print("Verifying captured source hashes...", flush=True)
        for profile_dir, _manifest in manifests:
            source_hashes[profile_dir] = verify_checksums(profile_dir)

    parity = {}
    with work_directory(args.keep_work) as workdir:
        for profile_dir, manifest in manifests:
            profile = manifest["profile"]
            print()
            with validation_test(validation_log, f"[{profile}] layout"):
                layout_dir = workdir / profile / "layout"
                layout_dir.mkdir(parents=True)
                layout_scenario = layout_probe_scenario(manifest)
                command = extraction_command(
                    launcher,
                    profile_dir,
                    layout_scenario,
                    layout_dir,
                    show_layout=True,
                )
                layout = run_command(
                    command,
                    layout_dir,
                    expect_success=True,
                    validation_log=validation_log,
                ).stdout
                for track in manifest["layout"]["tracks"]:
                    pattern = rf"^\s*{track['number']}\s+{track['kind']}\s+"
                    if not re.search(pattern, layout, re.MULTILINE):
                        raise ValidationError(
                            f"layout output omits Track {track['number']:02d}: {profile}"
                        )
                observed_offsets = None
                if (
                    profile in ("regular-audio", "data-last")
                    and "write_offset_probe" not in manifest
                ):
                    observed_offsets = probe_captured_write_offsets(
                        profile_dir,
                        layout_scenario,
                        layout_dir / "offset-probe",
                        validation_log,
                    )
                validate_capture_write_offsets(manifest, observed_offsets)

            for scenario in manifest["scenarios"]:
                test_name = f"[{profile}] {scenario['name']}"
                with validation_test(validation_log, test_name):
                    outputs = validate_scenario(
                        launcher,
                        profile_dir,
                        manifest,
                        scenario,
                        workdir / profile,
                        validation_log,
                    )
                    record_track_parity(
                        profile,
                        parity_candidates(scenario, outputs),
                        parity,
                        (
                            workdir / profile / "parity-baselines"
                            if args.keep_work is None
                            else None
                        ),
                    )
                    if args.keep_work is None:
                        shutil.rmtree(workdir / profile / scenario["name"])

    if not args.skip_source_hashes:
        print("\nRechecking captured source hashes...", flush=True)
        for profile_dir, before in source_hashes.items():
            after = verify_checksums(profile_dir)
            if after != before:
                raise ValidationError(
                    f"captured checksum manifest changed: {profile_dir.name}"
                )
    print(
        f"\nPASS: validated {len(manifests)} captured profile(s) without media; "
        f"skipped {len(missing)} profile(s)",
        flush=True,
    )


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        with validation_log_file(args.log_file) as validation_log:
            validate(args, validation_log)
    except ValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
