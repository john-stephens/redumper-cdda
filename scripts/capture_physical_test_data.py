#!/usr/bin/env python3
"""Capture scenario-sized redumper dumps for offline physical-disc tests."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = REPOSITORY / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from redumper_cdda.adapters.cdparanoia import CdparanoiaTocReader  # noqa: E402
from redumper_cdda.adapters.layout_provider import (  # noqa: E402
    ReconciledLayoutProvider,
)
from redumper_cdda.adapters.mmc import MmcTocReader  # noqa: E402
from redumper_cdda.adapters.redumper import RedumperCommandFactory  # noqa: E402
from redumper_cdda.adapters.subprocess_runner import SubprocessRunner  # noqa: E402
from redumper_cdda.adapters.workspace import Workspace  # noqa: E402
from redumper_cdda.application.output import OutputPlanner  # noqa: E402
from redumper_cdda.application.planning import ExtractionPlanner  # noqa: E402
from redumper_cdda.domain.disc import TrackKind  # noqa: E402
from redumper_cdda.domain.extraction import (  # noqa: E402
    ExtractionRequest,
    TrackSelection,
)
from redumper_cdda.integrity import (  # noqa: E402
    parse_media_errors,
    parse_split_write_offsets,
)


PROFILES = (
    "regular-audio",
    "track0-pregap",
    "data-first",
    "data-last",
    "data-only",
)
REQUIRED_DUMP_SUFFIXES = (".state", ".subcode", ".toc", ".fulltoc")
OFFSET_PROBE_SCENARIOS = {
    "regular-audio": "a03-a04-all-audio",
    "data-last": "d05-all-tracks",
}


class CaptureError(RuntimeError):
    """A physical capture could not be completed safely."""


class SilentReporter:
    def publish(self, _event):
        pass


@dataclass(frozen=True)
class Scenario:
    name: str
    cases: tuple
    selection: TrackSelection
    include_data: bool = False
    strict_accuraterip_track: object = None
    required_write_offset_sign: int = 0
    omitted_alignment_track: object = None
    fabricated_c2_track: object = None


def now():
    return datetime.now(timezone.utc).isoformat()


def parser():
    result = argparse.ArgumentParser(
        description=(
            "Capture all scenario-sized dump fixtures for one physical-test "
            "disc while that disc remains inserted."
        )
    )
    result.add_argument("profile", choices=PROFILES)
    result.add_argument("device", help="SCSI generic device, such as /dev/sg4")
    result.add_argument(
        "--output-root",
        type=Path,
        default=REPOSITORY / "test_data",
        help="capture root (default: repository test_data directory)",
    )
    result.add_argument(
        "--retries",
        type=nonnegative_int,
        help="redumper retries for each scenario dump (default: 100)",
    )
    return result


def nonnegative_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return number


def record_command(records, command, returncode, output):
    records.append(
        {
            "command": [str(item) for item in command],
            "exit_status": returncode,
            "output": str(output.relative_to(output.parents[1])),
        }
    )


def capture_command(command, output, records, merge_stderr=True):
    stderr = subprocess.STDOUT if merge_stderr else subprocess.PIPE
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=stderr)
    output.write_bytes(result.stdout)
    if not merge_stderr:
        output.with_suffix(output.suffix + ".stderr").write_bytes(result.stderr)
    record_command(records, command, result.returncode, output)
    if result.returncode != 0:
        raise CaptureError(
            f"command failed with status {result.returncode}: {' '.join(command)}"
        )
    return result.stdout


def stream_command(command, output, records, popen=subprocess.Popen, terminal=None):
    terminal = terminal or sys.stdout.buffer
    process = popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        with output.open("wb") as log:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                log.write(chunk)
                terminal.write(chunk)
                terminal.flush()
        returncode = process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        record_command(records, command, process.returncode, output)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    record_command(records, command, returncode, output)
    if returncode != 0:
        raise CaptureError(
            f"command failed with status {returncode}: {' '.join(command)}"
        )


def scenarios_for(
    profile,
    disc,
):
    tracks = disc.tracks
    audio = disc.audio_tracks()
    final = tracks[-1].number
    final_audio = audio[-1].number if audio else None

    if profile == "regular-audio":
        if len(tracks) < 3 or len(audio) != len(tracks) or tracks[0].begin_lba != 0:
            raise CaptureError(
                "regular-audio requires at least three audio tracks and Track 1 at LBA 0"
            )
        return (
            Scenario(
                "a02-track-01", ("A02 first", "F01 clean control"),
                TrackSelection(1, 1),
            ),
            Scenario(
                "a02-track-02",
                ("A02 middle", "F02 synthetic single-track errors"),
                TrackSelection(2, 2),
                fabricated_c2_track=2,
            ),
            Scenario("a02-final-track", ("A02 final",), TrackSelection(final, final)),
            Scenario(
                "a03-a04-all-audio",
                (
                    "A03", "A04", "A05 full/omitted", "A07 full",
                    "F03 synthetic range errors",
                ),
                TrackSelection(1, final),
                fabricated_c2_track=2,
            ),
            Scenario("a05-through-03", ("A05 -3",), TrackSelection(None, 3)),
            Scenario("a05-from-02", ("A05 2-",), TrackSelection(2, None)),
        )
    if profile == "track0-pregap":
        if not audio or len(audio) != len(tracks) or tracks[0].begin_lba <= 0:
            raise CaptureError(
                "track0-pregap requires an all-audio disc with Track 1 above LBA 0"
            )
        if final < 2:
            raise CaptureError("track0-pregap requires at least two tracks")
        return (
            Scenario("b02-track-00", ("B02", "B06 B02"), TrackSelection(0, 0)),
            Scenario("b03-track-01", ("B03", "B06 B03"), TrackSelection(1, 1)),
            Scenario("b04-track-00-01", ("B04", "B06 B04"), TrackSelection(0, 1)),
            Scenario("b05-all-audio", ("B05 full",), TrackSelection()),
            Scenario("b05-through-02", ("B05 -2",), TrackSelection(None, 2)),
        )
    if profile == "data-first":
        if (
            len(tracks) < 3
            or tracks[0].number != 1
            or tracks[0].kind is not TrackKind.DATA
            or any(track.kind is not TrackKind.AUDIO for track in tracks[1:])
        ):
            raise CaptureError(
                "data-first requires data Track 1 followed by at least two audio tracks"
            )
        return (
            Scenario("c01-all-audio", ("C01",), TrackSelection()),
            Scenario("c02-through-03", ("C02",), TrackSelection(None, 3)),
            Scenario(
                "c04-data-track-01",
                ("C04", "C04s", "C07 C04"),
                TrackSelection(1, 1),
                True,
            ),
            Scenario(
                "c05-mixed-01-03",
                ("C05", "C07 C05"),
                TrackSelection(1, 3),
                True,
            ),
        )
    if profile == "data-last":
        if (
            len(tracks) < 3
            or tracks[-1].kind is not TrackKind.DATA
            or any(track.kind is not TrackKind.AUDIO for track in tracks[:-1])
        ):
            raise CaptureError(
                "data-last requires at least two audio tracks followed by one "
                "final data track"
            )
        offset_target = tracks[-3].number
        result = (
            Scenario(
                "d01-offset-control",
                ("D08", "D11 control"),
                TrackSelection(offset_target, offset_target),
            ),
            Scenario("d02-final-audio", ("D02", "D06 D02"), TrackSelection(final_audio, final_audio)),
            Scenario("d03-all-audio", ("D03",), TrackSelection()),
            Scenario(
                "d04-final-audio-through-data",
                ("D04", "D06 D04"),
                TrackSelection(final_audio, final),
                True,
            ),
            Scenario(
                "d05-all-tracks",
                ("D05", "D06 D05", "D09", "D10", "D11 range", "D12"),
                TrackSelection(),
                True,
                strict_accuraterip_track=offset_target,
                required_write_offset_sign=-1,
                omitted_alignment_track=final_audio,
                fabricated_c2_track=final_audio,
            ),
            Scenario(
                "d07-final-data-track",
                ("D07", "D07s", "D06 D07"),
                TrackSelection(final, final),
                True,
            ),
        )
        return result
    if profile == "data-only":
        if any(track.kind is not TrackKind.DATA for track in tracks):
            raise CaptureError("data-only requires a disc containing only data tracks")
        result = [
            Scenario(
                "e03-data-track-01",
                ("E03", "E03s", "E06 E03"),
                TrackSelection(1, 1),
                True,
            )
        ]
        if final > 1:
            result.append(
                Scenario(
                    "e04-all-data",
                    ("E04", "E06 E04"),
                    TrackSelection(1, final),
                    True,
                )
            )
        return tuple(result)
    raise CaptureError(f"unknown profile: {profile}")


def request_for(device, scenario, retries):
    return ExtractionRequest(
        device=device,
        selection=scenario.selection,
        include_data=scenario.include_data,
        single_file=False,
        output=None,
        prefix="track",
        retries=retries,
        refine_passes=0,
        abort_on_skip=False,
        accuraterip=False,
    )


def capture_order(profile, scenarios):
    probe_name = OFFSET_PROBE_SCENARIOS.get(profile)
    if probe_name is None:
        return scenarios
    probe = next(item for item in scenarios if item.name == probe_name)
    return (probe,) + tuple(item for item in scenarios if item is not probe)


def layout_record(disc):
    return {
        "lead_out_lba": disc.lead_out_lba,
        "tracks": [
            {
                "number": track.number,
                "kind": track.kind.value,
                "control": track.control,
                "begin_lba": track.begin_lba,
                "end_lba": track.end_lba,
                "length_sectors": track.length_sectors,
                "begin_msf": track.begin_msf,
                "length_msf": track.length_msf,
            }
            for track in disc.tracks
        ],
    }


def read_captured_layout(toc_path, fulltoc_path, cdparanoia_path, device="-"):
    return ReconciledLayoutProvider(
        MmcTocReader(
            SubprocessRunner(), SilentReporter(), toc_path, fulltoc_path
        ),
        CdparanoiaTocReader(
            SubprocessRunner(), SilentReporter(), cdparanoia_path
        ),
    ).read(device)


def validate_dump(prefix):
    missing = [suffix for suffix in REQUIRED_DUMP_SUFFIXES if not Path(f"{prefix}{suffix}").is_file()]
    if not (Path(f"{prefix}.scram").is_file() or Path(f"{prefix}.scrap").is_file()):
        missing.append(".scram or .scrap")
    if missing:
        raise CaptureError(
            f"redumper dump {prefix} is incomplete; missing {', '.join(missing)}"
        )


def validate_clean_capture(log_path):
    output = log_path.read_text(encoding="utf-8", errors="replace")
    errors = parse_media_errors(output)
    if errors is None:
        raise CaptureError(
            f"could not determine SCSI/C2 status from {log_path}"
        )
    has_errors = errors["SCSI"] > 0 or errors["C2"] > 0
    if has_errors:
        raise CaptureError(
            "clean capture unexpectedly retained SCSI or C2 errors"
        )
    return errors


def probe_write_offsets(
    source_prefix,
    include_data,
    log_path,
    run=subprocess.run,
    temporary_directory=tempfile.TemporaryDirectory,
):
    with temporary_directory(prefix="redumper-cdda-offset-probe-") as value:
        workdir = Path(value)
        image_name = "offset-probe"
        Workspace(workdir).stage_existing_dump(source_prefix, image_name)
        command = [
            "redumper",
            "split",
            f"--image-path={workdir}",
            f"--image-name={image_name}",
            "--force-split",
        ]
        if include_data:
            command.append("--filesystem-trim")
        result = run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        log_path.write_bytes(result.stdout)
        if result.returncode != 0:
            raise CaptureError(
                "automatic write-offset probe failed with status "
                f"{result.returncode}"
            )
        try:
            offsets = parse_split_write_offsets(
                result.stdout.decode("utf-8", errors="replace")
            )
        except RuntimeError as exc:
            raise CaptureError(
                f"could not determine automatic split write offset: {exc}"
            ) from exc
    return tuple(offsets), tuple(command)


def validate_profile_write_offsets(profile, offsets):
    values = [offset for _lba, offset in offsets]
    if profile == "regular-audio" and any(values):
        raise CaptureError(
            f"{profile} requires an automatic split write offset of zero; "
            f"redumper reported {values}"
        )
    if profile == "data-last" and not any(offset < 0 for offset in values):
        raise CaptureError(
            "data-last requires a negative nonzero automatic split write offset"
        )


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_hashes(root):
    checksum_path = root / "SHA256SUMS"
    paths = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path != checksum_path
    )
    with checksum_path.open("w", encoding="utf-8") as stream:
        for path in paths:
            stream.write(f"{sha256_file(path)}  {path.relative_to(root)}\n")


def package_version():
    try:
        return metadata.version("redumper-cdda")
    except metadata.PackageNotFoundError:
        return "source checkout"


def repository_version():
    result = subprocess.run(
        ["git", "describe", "--always", "--dirty"],
        cwd=REPOSITORY,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def parse_inquiry(payload):
    if len(payload) < 36:
        raise CaptureError("SCSI INQUIRY response is shorter than 36 bytes")
    vendor = payload[8:16].decode("ascii", errors="replace").strip()
    product = payload[16:32].decode("ascii", errors="replace").strip()
    revision = payload[32:36].decode("ascii", errors="replace").strip()
    if not all(
        value and value.isprintable() for value in (vendor, product, revision)
    ):
        raise CaptureError("SCSI INQUIRY response lacks drive identification")
    return {
        "vendor": vendor,
        "model": product,
        "firmware_revision": revision,
    }


def capture(args):
    for executable in ("redumper", "cdparanoia", "sg_raw"):
        if shutil.which(executable) is None:
            raise CaptureError(f"{executable} not found")
    destination = args.output_root.resolve() / args.profile
    if destination.exists():
        raise CaptureError(
            f"capture directory already exists: {destination}; move or remove it explicitly"
        )
    destination.mkdir(parents=True)
    records = []
    manifest = {
        "format": 1,
        "profile": args.profile,
        "device": args.device,
        "redumper_cdda_version": package_version(),
        "repository_version": repository_version(),
        "started_at": now(),
        "status": "incomplete",
        "commands": records,
        "scenarios": [],
    }
    retries = args.retries
    if retries is None:
        retries = 100
    manifest["retries"] = retries
    manifest_path = destination / "manifest.json"
    try:
        capture_command(
            ["redumper", "--version"],
            destination / "redumper-version.txt",
            records,
        )
        capture_command(
            ["cdparanoia", "--version"],
            destination / "cdparanoia-version.txt",
            records,
        )
        inquiry = capture_command(
            [
                "sg_raw", "--readonly", "--binary", "--request=96",
                args.device, "12", "00", "00", "00", "60", "00",
            ],
            destination / "drive-inquiry.bin",
            records,
            False,
        )
        manifest["drive"] = parse_inquiry(inquiry)
        toc_path = destination / "live.toc"
        fulltoc_path = destination / "live.fulltoc"
        capture_command(MmcTocReader.command(args.device), toc_path, records, False)
        capture_command(
            MmcTocReader.full_toc_command(args.device),
            fulltoc_path,
            records,
            False,
        )
        cdparanoia_path = destination / "cdparanoia-toc.txt"
        if args.profile == "data-only":
            cdparanoia_path.write_text(
                "cdparanoia TOC query intentionally skipped: MMC reports no audio tracks.\n",
                encoding="utf-8",
            )
        else:
            capture_command(
                CdparanoiaTocReader.command(args.device),
                cdparanoia_path,
                records,
            )

        disc = read_captured_layout(
            toc_path, fulltoc_path, cdparanoia_path, args.device
        )
        manifest["layout"] = layout_record(disc)
        planner = ExtractionPlanner(OutputPlanner(), RedumperCommandFactory)

        scenarios = scenarios_for(args.profile, disc)
        for scenario in capture_order(args.profile, scenarios):
            scenario_dir = destination / scenario.name
            scenario_dir.mkdir()
            plan = planner.create(
                request_for(args.device, scenario, retries),
                disc,
                scenario_dir,
            )
            print(
                f"\n[{scenario.name}] LBA {plan.dump_start_lba}.."
                f"{plan.dump_end_lba} ({plan.physical_range.sectors} sectors)",
                flush=True,
            )
            scenario_record = {
                "name": scenario.name,
                "physical_test_cases": list(scenario.cases),
                "existing_dump": str(
                    (scenario_dir / plan.image_name).relative_to(destination)
                ),
                "cdparanoia_toc_file": "cdparanoia-toc.txt",
                "selected_tracks": [
                    track.number for track in plan.selection.tracks
                ],
                "include_data": scenario.include_data,
                "logical_start_lba": plan.logical_start_lba,
                "logical_end_lba": plan.logical_end_lba,
                "dump_start_lba": plan.dump_start_lba,
                "dump_end_lba": plan.dump_end_lba,
                "expected_media_errors": False,
                "strict_accuraterip_track": scenario.strict_accuraterip_track,
                "required_write_offset_sign": scenario.required_write_offset_sign,
                "omitted_alignment_track": scenario.omitted_alignment_track,
                "fabricated_c2_track": scenario.fabricated_c2_track,
                "status": "incomplete",
            }
            manifest["scenarios"].append(scenario_record)
            stream_command(
                plan.dump_command,
                scenario_dir / "redumper-dump.log",
                records,
            )
            prefix = scenario_dir / plan.image_name
            validate_dump(prefix)
            errors = validate_clean_capture(scenario_dir / "redumper-dump.log")
            scenario_record["media_errors"] = errors
            dumped_layout = read_captured_layout(
                Path(f"{prefix}.toc"),
                Path(f"{prefix}.fulltoc"),
                cdparanoia_path,
            )
            if dumped_layout != disc:
                raise CaptureError(
                    f"redumper TOCs for {scenario.name} differ from the live layout"
                )
            scenario_record["status"] = "complete"
            probe_name = OFFSET_PROBE_SCENARIOS.get(args.profile)
            if scenario.name == probe_name:
                probe_log = destination / "write-offset-probe.log"
                offsets, command = probe_write_offsets(
                    prefix,
                    scenario.include_data,
                    probe_log,
                )
                validate_profile_write_offsets(args.profile, offsets)
                manifest["write_offset_probe"] = {
                    "scenario": probe_name,
                    "command": list(command),
                    "output": probe_log.name,
                    "offsets": [list(item) for item in offsets],
                }

        manifest["status"] = "complete"
        manifest["completed_at"] = now()
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print("\nHashing captured files...", flush=True)
        write_hashes(destination)
        print(f"Capture complete: {destination}")
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["completed_at"] = now()
        manifest["error"] = str(exc)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        capture(args)
    except CaptureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
