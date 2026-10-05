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
from dataclasses import dataclass
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from redumper_cdda.integrity import (  # noqa: E402
    inspect_track_media_errors,
    parse_split_write_offsets,
    state_offset_for_lba,
)


DEFAULT_TEST_DATA = REPOSITORY / "test_data"
DEFAULT_LAUNCHER = REPOSITORY / "redumper-cdda"
PROJECT_PYTHON = REPOSITORY / ".venv" / "bin" / "python"
PCM_BYTES_PER_SECTOR = 2352
PCM_FRAMES_PER_SECTOR = 588


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

    def finish_test(self, name, passed):
        separator = "-" * 80
        result = "PASS" if passed else "FAIL"
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
        validation_log.finish_test(name, passed=False)
        print("FAIL", flush=True)
        raise
    else:
        validation_log.finish_test(name, passed=True)
        print("PASS", flush=True)


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
    found = {manifest["profile"] for _directory, manifest in manifests}
    missing = sorted(requested - found)
    if missing:
        raise ValidationError("captured profiles not found: " + ", ".join(missing))
    if not manifests:
        raise ValidationError(f"no captured profiles found under {test_data}")
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


def iso_hash(path):
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
            wave_pcm_hash(path, sectors) if kind == "audio" else iso_hash(path)
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

    if scenario.get("expected_media_errors", False):
        strict_dir = scenario_dir / "strict"
        strict_dir.mkdir()
        command = extraction_command(
            launcher, profile_dir, scenario, strict_dir, abort_on_skip=True
        )
        run_command(command, strict_dir, expect_success=False, validation_log=validation_log)
        actual = set(strict_dir.glob("*.wav")) | set(strict_dir.glob("*.iso"))
        all_paths = {item[0].name for item in expected}
        actual_names = {path.name for path in actual}
        if not actual_names < all_paths:
            raise ValidationError(
                f"strict error policy did not omit affected output: {scenario['name']}"
            )
        known_clean = clean_tracks(manifest) & set(scenario["selected_tracks"])
        clean_names = {
            f"track{number:02d}."
            f"{'wav' if selected_track(manifest, number)['kind'] == 'audio' else 'iso'}"
            for number in known_clean
        }
        if not clean_names <= actual_names:
            raise ValidationError(
                f"strict error policy omitted a known-clean track: {scenario['name']}"
            )
        if len(tracks) > 1 and all_audio:
            strict_single_dir = scenario_dir / "strict-single"
            strict_single_dir.mkdir()
            command = extraction_command(
                launcher,
                profile_dir,
                scenario,
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
    if shutil.which("redumper") is None:
        raise ValidationError("redumper not found; offline splitting still requires it")
    manifests = load_manifests(args.test_data.resolve(), args.profile)
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
                layout_scenario = manifest["scenarios"][0]
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
                    if not scenario.get("expected_media_errors", False):
                        for number, output in outputs.items():
                            parity.setdefault((profile, number), []).append(output)

        for (profile, number), outputs in parity.items():
            validate_track_parity(profile, number, outputs)

    if not args.skip_source_hashes:
        print("\nRechecking captured source hashes...", flush=True)
        for profile_dir, before in source_hashes.items():
            after = verify_checksums(profile_dir)
            if after != before:
                raise ValidationError(
                    f"captured checksum manifest changed: {profile_dir.name}"
                )
    print(
        f"\nPASS: validated {len(manifests)} captured profile(s) without media",
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
