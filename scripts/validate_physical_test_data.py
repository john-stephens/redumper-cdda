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
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATA = REPOSITORY / "test_data"
DEFAULT_LAUNCHER = REPOSITORY / "redumper-cdda"
PCM_BYTES_PER_SECTOR = 2352
PCM_FRAMES_PER_SECTOR = 588


class ValidationError(RuntimeError):
    """Captured data or an offline extraction failed validation."""


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
):
    prefix = safe_child(
        profile_dir, scenario["existing_dump"], "existing dump"
    )
    toc = safe_child(
        profile_dir, scenario["cdparanoia_toc_file"], "cdparanoia TOC"
    )
    command = [str(launcher)]
    if show_layout:
        command.append("--show-layout")
    else:
        command.append(selection_text(scenario["selected_tracks"]))
    command.extend(
        [
            f"--existing-dump={prefix}",
            f"--cdparanoia-toc-file={toc}",
            "--no-accuraterip",
        ]
    )
    if scenario.get("include_data"):
        command.append("--include-data")
    if single_file:
        command.append("--single-file")
    if abort_on_skip:
        command.append("--abort-on-skip")
    if not show_layout:
        command.append(f"--log-file={output_dir / 'validation.log'}")
    return command


def run_command(command, cwd, expect_success):
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
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


def clean_tracks(manifest):
    return {
        number
        for scenario in manifest["scenarios"]
        if not scenario.get("expected_media_errors", False)
        for number in scenario["selected_tracks"]
    }


def validate_scenario(launcher, profile_dir, manifest, scenario, workdir):
    scenario_dir = workdir / scenario["name"]
    separate_dir = scenario_dir / "separate"
    separate_dir.mkdir(parents=True)
    command = extraction_command(
        launcher, profile_dir, scenario, separate_dir
    )
    run_command(command, separate_dir, expect_success=True)
    assert_offline_log(separate_dir / "validation.log")
    expected = expected_outputs(manifest, scenario, separate_dir)
    separate_hashes = validate_output_set(expected)

    tracks = [selected_track(manifest, number) for number in scenario["selected_tracks"]]
    all_audio = all(track["kind"] == "audio" for track in tracks)
    if len(tracks) > 1 and all_audio:
        single_dir = scenario_dir / "single"
        single_dir.mkdir()
        command = extraction_command(
            launcher, profile_dir, scenario, single_dir, single_file=True
        )
        run_command(command, single_dir, expect_success=True)
        assert_offline_log(single_dir / "validation.log")
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
            launcher, profile_dir, scenario, single_dir, single_file=True
        )
        run_command(command, single_dir, expect_success=True)
        assert_offline_log(single_dir / "validation.log")
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
        run_command(command, invalid_dir, expect_success=False)
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
        run_command(command, strict_dir, expect_success=False)
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
            run_command(command, strict_single_dir, expect_success=False)
            if list(strict_single_dir.glob("*.wav")):
                raise ValidationError(
                    f"strict combined mode created output: {scenario['name']}"
                )
    return separate_hashes


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


def validate(args):
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
            print(f"\n[{profile}] layout", flush=True)
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
            layout = run_command(command, layout_dir, expect_success=True).stdout
            for track in manifest["layout"]["tracks"]:
                pattern = rf"^\s*{track['number']}\s+{track['kind']}\s+"
                if not re.search(pattern, layout, re.MULTILINE):
                    raise ValidationError(
                        f"layout output omits Track {track['number']:02d}: {profile}"
                    )

            for scenario in manifest["scenarios"]:
                print(f"[{profile}] {scenario['name']}", flush=True)
                hashes = validate_scenario(
                    launcher,
                    profile_dir,
                    manifest,
                    scenario,
                    workdir / profile,
                )
                if not scenario.get("expected_media_errors", False):
                    for number, digest in hashes.items():
                        parity.setdefault((profile, number), set()).add(digest)

        for (profile, number), hashes in parity.items():
            if len(hashes) != 1:
                raise ValidationError(
                    f"Track {number:02d} differs across {profile} scenarios"
                )

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
        validate(args)
    except ValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
