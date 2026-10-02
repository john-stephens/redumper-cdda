"""Redumper process control and extraction workflow orchestration."""

import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .accuraterip import (
    accuraterip_layout_type,
    print_accuraterip_report,
    verify_with_accuraterip,
)
from .cue import (
    changed_files,
    identify_generated_audio_segments,
    identify_generated_data_track,
    snapshot_files,
)
from .integrity import (
    data_errors_present,
    inspect_track_media_errors,
    parse_media_errors,
    parse_split_write_offsets,
    print_media_errors,
    should_abort_on_errors,
)
from .layout import (
    format_track_numbers,
    parse_cdparanoia_toc,
    parse_mmc_toc,
    reconcile_disc_layout,
    resolve_disc_selection,
    sectors_to_msf,
    validate_data_output_mode,
)
from .outputs import (
    build_output_jobs,
    create_output_files,
)


MMC_TOC_ALLOCATION_LENGTH = 804
END_PADDING_SECTORS = 1


@dataclass(frozen=True)
class ExtractionPlan:
    """Deterministic paths, ranges, commands, and outputs for one extraction."""

    disc_tracks: list
    selected_tracks: list
    workdir: Path
    first_track: dict
    last_track: dict
    track_label: str
    logical_start_lba: int
    logical_end_lba: int
    expected_sectors: int
    dump_start_lba: int
    dump_end_lba: int
    image_name: str
    output_jobs: list
    dump_command: list
    refine_command: list
    split_command: list


@dataclass(frozen=True)
class AcquisitionResult:
    """Final redumper integrity state after bounded dump/refinement."""

    errors: dict
    refine_passes_used: int


class TerminationRequested(Exception):
    def __init__(self, signum):
        self.signum = signum
        self.signal_name = signal.Signals(signum).name
        super().__init__(self.signal_name)


def quote_command(command):
    return " ".join(
        subprocess.list2cmdline([str(arg)])
        for arg in command
    )


def stop_process(process):
    if process.poll() is not None:
        return

    try:
        process.terminate()
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def wait_for_process(process):
    try:
        return process.wait()
    except BaseException:
        stop_process(process)
        raise


def communicate_with_process(process):
    try:
        return process.communicate()
    except BaseException:
        stop_process(process)

        for stream in (
            process.stdout,
            process.stderr,
        ):
            if stream is not None:
                stream.close()

        raise


def track_number_for_lba(tracks, lba):
    for track in tracks:
        if (
            track["begin"]
            <= lba
            < track["end"]
        ):
            return track["number"]

    if tracks and lba >= tracks[-1]["end"]:
        return tracks[-1]["number"]

    if tracks and lba < tracks[0]["begin"]:
        return tracks[0]["number"]

    return None


def format_progress_label(
    progress_label,
    current_track,
    progress_tracks,
):
    if current_track is not None:
        current_kind = next(
            (
                track.get("kind", "audio")
                for track in progress_tracks or []
                if track["number"] == current_track
            ),
            "audio",
        )

        if current_kind == "data":
            return (
                f"{progress_label} data track "
                f"{current_track:02d}"
            )

        return (
            f"{progress_label} track "
            f"{current_track:02d}"
        )

    if progress_tracks:
        return f"{progress_label} data"

    return progress_label


def run_command_capture(
    command,
    verbose=False,
    progress_label=None,
    progress_tracks=None,
):
    """
    Run a command while displaying its output live and also
    retaining the output for later parsing.

    stderr is merged into stdout because redumper may use either
    stream for status/progress information.
    """

    if verbose:
        print()
        print("+ " + quote_command(command))
        print()
    elif progress_label:
        current_track = (
            progress_tracks[0]["number"]
            if progress_tracks
            else None
        )
        display_label = format_progress_label(
            progress_label,
            current_track,
            progress_tracks,
        )
        print(
            f"{display_label}:   0%",
            end="",
            flush=True,
        )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    lines = []
    last_progress = (
        0,
        (
            progress_tracks[0]["number"]
            if progress_tracks
            else None
        ),
    )

    assert process.stdout is not None

    try:
        for line in process.stdout:
            if verbose:
                print(line, end="")
            elif progress_label:
                percentages = re.findall(
                    r"\[\s*(\d+)%\]",
                    line,
                )

                if percentages:
                    percent = int(
                        percentages[-1]
                    )

                    lba_match = re.search(
                        r"\bLBA\s*:\s*(-?\d+)",
                        line,
                        re.IGNORECASE,
                    )
                    current_track = last_progress[1]

                    if lba_match and progress_tracks:
                        current_track = track_number_for_lba(
                            progress_tracks,
                            int(lba_match.group(1)),
                        )

                    progress = (
                        percent,
                        current_track,
                    )

                    if progress != last_progress:
                        display_label = format_progress_label(
                            progress_label,
                            current_track,
                            progress_tracks,
                        )
                        print(
                            f"\r{display_label}: "
                            f"{percent:3d}%",
                            end="",
                            flush=True,
                        )
                        last_progress = progress

            lines.append(line)

        returncode = wait_for_process(
            process
        )
    except BaseException:
        stop_process(process)
        raise
    finally:
        process.stdout.close()

        if not verbose and progress_label:
            print()

    return (
        returncode,
        "".join(lines),
    )


def run_command(
    command,
    verbose=False,
    status_label=None,
):
    if verbose:
        print()
        print("+ " + quote_command(command))
        print()
    elif status_label:
        print(
            f"{status_label}...",
            end="",
            flush=True,
        )

    process = subprocess.Popen(
        command,
        stdout=(
            None
            if verbose
            else subprocess.PIPE
        ),
        stderr=(
            None
            if verbose
            else subprocess.STDOUT
        ),
        text=not verbose,
    )

    if verbose:
        returncode = wait_for_process(
            process
        )
        output = None
    else:
        output, _ = communicate_with_process(
            process
        )
        returncode = process.returncode

        if status_label:
            print(
                " done"
                if returncode == 0
                else " failed"
            )

    if returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            command,
        )

    return subprocess.CompletedProcess(
        command,
        returncode,
        output,
    )


def read_mmc_toc(device, verbose=False):
    allocation_msb = (
        MMC_TOC_ALLOCATION_LENGTH >> 8
    ) & 0xFF
    allocation_lsb = (
        MMC_TOC_ALLOCATION_LENGTH
        & 0xFF
    )
    command = [
        "sg_raw",
        "--readonly",
        "--binary",
        f"--request={MMC_TOC_ALLOCATION_LENGTH}",
        device,
        "43",  # READ TOC/PMA/ATIP
        "00",  # LBA addresses, TOC format 0
        "00",
        "00",
        "00",
        "00",  # start track
        "00",
        f"{allocation_msb:02x}",
        f"{allocation_lsb:02x}",
        "00",
    ]

    if verbose:
        print(
            "Reading complete track layout with MMC READ TOC..."
        )

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = communicate_with_process(
        process
    )

    if process.returncode != 0:
        details = stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()
        raise RuntimeError(
            "MMC READ TOC failed"
            + (
                "\n\n" + details
                if details
                else ""
            )
        )

    return parse_mmc_toc(
        stdout
    )


def read_disc_toc(device, verbose=False):
    command = [
        "cdparanoia",
        "-Q",
        "-d",
        device,
    ]

    if verbose:
        print(
            "Reading audio track layout with cdparanoia..."
        )

    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout, stderr = communicate_with_process(
        process
    )

    result = subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )

    if result.returncode != 0:
        details = "\n".join(
            part
            for part in (
                result.stdout.strip(),
                result.stderr.strip(),
            )
            if part
        )

        raise RuntimeError(
            "cdparanoia -Q failed"
            + (
                "\n\n" + details
                if details
                else ""
            )
        )

    output = (
        (result.stdout or "")
        + "\n"
        + (result.stderr or "")
    )

    return parse_cdparanoia_toc(
        output
    )


def read_disc_layout(device, verbose=False):
    mmc_tracks = read_mmc_toc(
        device,
        verbose=verbose,
    )

    if not any(
        track["kind"] == "audio"
        for track in mmc_tracks
    ):
        return mmc_tracks

    cdparanoia_tracks = read_disc_toc(
        device,
        verbose=verbose,
    )

    return reconcile_disc_layout(
        mmc_tracks,
        cdparanoia_tracks,
    )


def print_selected_track(track):
    print()
    print("Selected track")
    print("--------------")

    print(
        f"Track:               "
        f"{track['number']:02d}"
    )
    print(
        f"Type:                "
        f"{track.get('kind', 'audio')}"
    )

    print(
        f"Begin LBA:           "
        f"{track['begin']}"
    )

    print(
        f"Length:              "
        f"{track['length']:,} sectors"
    )

    print(
        f"Length (MSF):        "
        f"{track['length_msf']}"
    )

    print(
        f"Logical end LBA:     "
        f"{track['end']} [exclusive]"
    )

    print(
        f"Calculated duration: "
        f"{sectors_to_msf(track['length'])}"
    )


def build_extraction_plan(args, workdir, disc_tracks, selected_tracks):
    """Build the complete extraction plan without running external commands."""

    first_track = selected_tracks[0]
    last_track = selected_tracks[-1]
    logical_start_lba = first_track["begin"]
    logical_end_lba = last_track["end"]
    dump_start_lba = logical_start_lba
    dump_end_lba = logical_end_lba + END_PADDING_SECTORS
    image_name = (
        f"track{first_track['number']:02d}"
        if len(selected_tracks) == 1
        else (
            f"tracks{first_track['number']:02d}-"
            f"{last_track['number']:02d}"
        )
    )
    output_jobs = build_output_jobs(
        selected_tracks,
        args.single_file,
        output=args.output,
        prefix=args.prefix,
    )

    for job in output_jobs:
        job["temporary_path"] = job["output_path"].with_name(
            f".{job['output_path'].name}.part"
        )

    common_range = [
        f"--drive={args.device}",
        f"--image-path={workdir}",
        f"--image-name={image_name}",
        f"--retries={args.retries}",
        f"--lba-start={dump_start_lba}",
        f"--lba-end={dump_end_lba}",
    ]
    split_command = [
        "redumper",
        "split",
        f"--image-path={workdir}",
        f"--image-name={image_name}",
        "--force-split",
    ]

    if any(
        track.get("kind", "audio") == "data"
        for track in selected_tracks
    ):
        split_command.append("--filesystem-trim")

    return ExtractionPlan(
        disc_tracks=disc_tracks,
        selected_tracks=selected_tracks,
        workdir=workdir,
        first_track=first_track,
        last_track=last_track,
        track_label=format_track_numbers(selected_tracks),
        logical_start_lba=logical_start_lba,
        logical_end_lba=logical_end_lba,
        expected_sectors=sum(track["length"] for track in selected_tracks),
        dump_start_lba=dump_start_lba,
        dump_end_lba=dump_end_lba,
        image_name=image_name,
        output_jobs=output_jobs,
        dump_command=["redumper", "dump", *common_range],
        refine_command=["redumper", "refine", *common_range],
        split_command=split_command,
    )


def acquire_and_refine(plan, args, concise):
    """Run one bounded dump and only the refinement passes it requires."""

    if args.verbose:
        print()
        print("Initial partial dump")
        print("====================")

    returncode, output = run_command_capture(
        plan.dump_command,
        verbose=args.verbose,
        progress_label="Reading" if concise else None,
        progress_tracks=plan.selected_tracks,
    )

    if returncode != 0:
        sys.exit(
            "ERROR: redumper dump failed.\n"
            "No output file was created."
        )

    errors = parse_media_errors(output)
    if errors is None:
        sys.exit(
            "ERROR: Could not determine redumper "
            "SCSI/C2 error status from dump output.\n"
            "Refusing to create output because dump "
            "integrity cannot be verified."
        )

    if args.verbose:
        print_media_errors(errors)

    refine_passes_used = 0
    while data_errors_present(errors) and refine_passes_used < args.refine_passes:
        refine_passes_used += 1

        if args.verbose:
            print()
            print(
                f"Refine pass {refine_passes_used}/"
                f"{args.refine_passes}"
            )
            print("================")
            print(
                f"Refining only LBA {plan.dump_start_lba}.."
                f"{plan.dump_end_lba} [end exclusive]"
            )

        returncode, output = run_command_capture(
            plan.refine_command,
            verbose=args.verbose,
            progress_label=(
                f"Refining {refine_passes_used}/{args.refine_passes}"
                if concise
                else None
            ),
            progress_tracks=plan.selected_tracks,
        )

        if returncode != 0:
            sys.exit(
                "ERROR: redumper refine failed.\n"
                "No output file was created."
            )

        errors = parse_media_errors(output)
        if errors is None:
            sys.exit(
                "ERROR: Could not determine redumper "
                "SCSI/C2 error status after refine.\n"
                "No output file was created."
            )

        if args.verbose:
            print_media_errors(errors)

    unresolved_errors = data_errors_present(errors)
    if (
        should_abort_on_errors(errors, args.abort_on_skip)
        and args.single_file
    ):
        sys.exit(
            "ERROR: SCSI/C2 errors remain after "
            f"{refine_passes_used} refine pass"
            f"{'' if refine_passes_used == 1 else 'es'}.\n"
            f"Remaining SCSI errors: {errors['SCSI']}\n"
            f"Remaining C2 errors:   {errors['C2']}\n"
            "No output file was created because "
            "--abort-on-skip was specified with "
            "--single-file."
        )

    if unresolved_errors and concise and not args.abort_on_skip:
        print(
            "Warning: writing output with unresolved "
            f"SCSI={errors['SCSI']}, C2={errors['C2']}"
        )

    if args.verbose:
        print()
        print("Data integrity")
        print("==============")
        print()
        print(f"SCSI: {errors['SCSI']}")
        print(f"C2:   {errors['C2']}")
        print(
            f"Q:    {errors['Q']} "
            "(reported, not used as audio-data failure criterion)"
        )
        print()
        if unresolved_errors:
            print(
                "WARNING: unresolved SCSI/C2 errors "
                "will be included in the forced split."
            )
        else:
            print(
                f"PASS after {refine_passes_used} refine pass"
                f"{'' if refine_passes_used == 1 else 'es'}."
            )

    return AcquisitionResult(
        errors=errors,
        refine_passes_used=refine_passes_used,
    )


def split_and_filter_outputs(
    plan,
    args,
    workdir,
    unresolved_errors,
    before,
    concise,
):
    """Force-split the partial image and omit only error-affected jobs."""

    if args.verbose:
        print()
        print("Splitting partial dump")
        print("======================")

    if concise:
        print("Splitting...", end="", flush=True)

    split_returncode, split_output = run_command_capture(
        plan.split_command,
        verbose=args.verbose,
    )

    if concise:
        print(" done" if split_returncode == 0 else " failed")

    if split_returncode != 0:
        sys.exit(
            "ERROR: redumper could not split the "
            "partial dump even with --force-split.\n"
            f"Exit status: {split_returncode}\n"
            "No output file was created."
        )

    output_jobs = plan.output_jobs
    skipped_error_jobs = []
    if unresolved_errors and args.abort_on_skip and not args.single_file:
        try:
            write_offsets = parse_split_write_offsets(split_output)
            per_track_errors = inspect_track_media_errors(
                workdir / f"{plan.image_name}.state",
                plan.selected_tracks,
                write_offsets,
            )
        except RuntimeError as exc:
            sys.exit(
                f"ERROR: {exc}\n"
                "Could not safely identify which track outputs "
                "contain unresolved SCSI/C2 errors.\n"
                "No output file was created."
            )

        clean_output_jobs = []
        for job in output_jobs:
            track_errors = per_track_errors[job["track"]["number"]]
            if data_errors_present(track_errors):
                job["media_errors"] = track_errors
                skipped_error_jobs.append(job)
            else:
                clean_output_jobs.append(job)
        output_jobs = clean_output_jobs

        if not args.quiet:
            for job in skipped_error_jobs:
                track_errors = job["media_errors"]
                print(
                    f"Skipping Track {job['track']['number']:02d}: "
                    f"SCSI={track_errors['SCSI']} samples in "
                    f"{track_errors['SCSI sectors']} sectors, "
                    f"C2={track_errors['C2']} samples in "
                    f"{track_errors['C2 sectors']} sectors"
                )

        if not output_jobs:
            sys.exit(
                "ERROR: every selected track contains unresolved "
                "SCSI/C2 errors; no output file was created."
            )

    return output_jobs, skipped_error_jobs, changed_files(workdir, before)


def discover_output_sources(plan, output_jobs, changed, accuraterip_enabled):
    """Resolve split CUE/BIN sources for every permitted output job."""

    track_zero_sectors = (
        plan.first_track["length"]
        if plan.first_track["number"] == 0
        else None
    )
    accuraterip_tracks = []

    for job in output_jobs:
        if job["kind"] == "data":
            job["data_track"] = identify_generated_data_track(
                plan.workdir,
                plan.image_name,
                changed,
                job["track"]["number"],
            )
            job["cue_path"] = job["data_track"]["cue_path"]
            job["pregap_skipped"] = job["data_track"]["start_sector"]
            continue

        job["segments"] = []
        job["pregap_skipped"] = None
        for component_track in job["component_tracks"]:
            (
                component_segments,
                job["cue_path"],
                component_pregap_skipped,
            ) = identify_generated_audio_segments(
                plan.workdir,
                plan.image_name,
                changed,
                component_track["number"],
                component_track["length"],
                track_zero_sectors=track_zero_sectors,
            )
            job["segments"].extend(component_segments)

            if accuraterip_enabled and component_track["number"] != 0:
                accuraterip_tracks.append(
                    {
                        "track": component_track,
                        "segments": component_segments,
                    }
                )

            if job["pregap_skipped"] is None:
                job["pregap_skipped"] = component_pregap_skipped

    return accuraterip_tracks


def extract_track(args, workdir):

    concise = (
        not args.verbose
        and not args.quiet
    )

    # --------------------------------------------------------------
    # Track layout
    # --------------------------------------------------------------

    try:
        disc_tracks = read_disc_layout(
            args.device,
            verbose=args.verbose,
        )
        selected_tracks = resolve_disc_selection(
            disc_tracks,
            args.track,
            include_data=args.include_data,
        )

        validate_data_output_mode(
            selected_tracks,
            args.include_data,
            args.single_file,
        )

        if getattr(
            args,
            "accuraterip",
            False,
        ):
            accuraterip_layout_type(
                disc_tracks
            )

            if not any(
                track.get("kind", "audio") == "audio"
                and track["number"] != 0
                for track in selected_tracks
            ):
                raise RuntimeError(
                    "The selection contains no AccurateRip-verifiable "
                    "audio tracks; Track 0 and data tracks are not tracked."
                )

    except RuntimeError as exc:
        sys.exit(
            f"ERROR: {exc}"
        )

    if args.verbose:
        for track in selected_tracks:
            print_selected_track(
                track
            )

    plan = build_extraction_plan(
        args,
        workdir,
        disc_tracks,
        selected_tracks,
    )
    first_track = plan.first_track
    track_label = plan.track_label
    logical_start_lba = plan.logical_start_lba
    logical_end_lba = plan.logical_end_lba
    expected_sectors = plan.expected_sectors
    dump_start_lba = plan.dump_start_lba
    dump_end_lba = plan.dump_end_lba
    image_name = plan.image_name
    output_jobs = plan.output_jobs

    output_paths = [
        job["output_path"]
        for job in output_jobs
    ]

    if concise:
        print(
            f"Ripping track{'' if len(selected_tracks) == 1 else 's'} "
            f"{track_label} from "
            f"sector {logical_start_lba} to "
            f"{logical_end_lba - 1}"
        )

        if not args.single_file:
            print(
                f"Output: {len(output_paths)} separate files"
            )
        else:
            print(
                f"Output: {output_paths[0]}"
            )

    before = snapshot_files(
        workdir
    )

    for output_path in output_paths:
        if output_path.exists():
            output_path.unlink()

    for job in output_jobs:
        temporary_path = job["temporary_path"]
        if temporary_path.exists():
            temporary_path.unlink()

    acquisition = acquire_and_refine(plan, args, concise)
    errors = acquisition.errors
    refine_passes_used = acquisition.refine_passes_used
    unresolved_errors = data_errors_present(errors)

    output_jobs, skipped_error_jobs, changed = split_and_filter_outputs(
        plan,
        args,
        workdir,
        unresolved_errors,
        before,
        concise,
    )

    try:
        accuraterip_tracks = discover_output_sources(
            plan,
            output_jobs,
            changed,
            getattr(args, "accuraterip", False),
        )
    except RuntimeError as exc:
        sys.exit(
            f"ERROR: {exc}\n"
            "No output file was created."
        )

    # --------------------------------------------------------------
    # Convert to temporary WAV/ISO files, then commit the full set.
    # --------------------------------------------------------------

    try:
        if concise:
            print(
                (
                    f"Writing {len(output_jobs)} output files..."
                    if not args.single_file
                    else (
                        "Writing ISO..."
                        if output_jobs[0]["kind"] == "data"
                        else "Writing WAV..."
                    )
                ),
                end="",
                flush=True,
            )

        create_output_files(
            output_jobs,
            verbose=args.verbose,
        )

        if concise:
            print(" done")

    except BaseException as exc:

        if concise:
            print(" failed")

        if isinstance(
            exc,
            (KeyboardInterrupt, TerminationRequested),
        ):
            raise

        sys.exit(
            f"ERROR: output creation failed: {exc}"
        )

    # --------------------------------------------------------------
    # AccurateRip verification. Output creation is already
    # complete, so lookup failures or checksum mismatches never remove
    # successfully written WAV/ISO files.
    # --------------------------------------------------------------

    if getattr(
        args,
        "accuraterip",
        False,
    ):
        if concise:
            print(
                "Checking AccurateRip...",
                flush=True,
            )

        try:
            accuraterip_report = verify_with_accuraterip(
                disc_tracks,
                accuraterip_tracks,
                workdir,
            )
        except RuntimeError as exc:
            sys.exit(
                f"ERROR: AccurateRip verification failed: {exc}\n"
                "Completed output files were retained."
            )

        if not args.quiet:
            print_accuraterip_report(
                accuraterip_report,
                verbose=args.verbose,
            )

    # --------------------------------------------------------------
    # Complete
    # --------------------------------------------------------------

    if args.verbose:
        print()
        print("Complete")
        print("========")

        print(
            f"Tracks:             "
            f"{track_label}"
        )

        print(
            f"Logical LBA range:  "
            f"{logical_start_lba}.."
            f"{logical_end_lba}"
        )

        print(
            f"Physical read range:"
            f" {dump_start_lba}.."
            f"{dump_end_lba}"
        )

        print(
            f"Selected sectors:   "
            f"{expected_sectors:,}"
        )

        print(
            f"Refine passes used: "
            f"{refine_passes_used}"
        )

        print(
            f"Final SCSI errors:  "
            f"{errors['SCSI']}"
        )

        print(
            f"Final C2 errors:    "
            f"{errors['C2']}"
        )

        print(
            f"Final Q errors:     "
            f"{errors['Q']}"
        )

        for job in output_jobs:
            print(
                f"Track {job['track']['number']:02d} BIN offset: "
                f"{job['pregap_skipped']:,} sectors"
                if job["track"] is not None
                else (
                    f"Initial BIN offset: "
                    f"{job['pregap_skipped']:,} sectors"
                )
            )
            print(
                f"Temporary CUE:      "
                f"{job['cue_path']}"
            )
            print(
                f"{'ISO' if job['kind'] == 'data' else 'WAV'}:"
                f"                {job['output_path']}"
            )

        print()
        if unresolved_errors:
            print(
                "Integrity:          WARNING "
                f"(SCSI={errors['SCSI']}, "
                f"C2={errors['C2']})"
            )
        else:
            print(
                "Integrity:          PASS "
                "(SCSI=0, C2=0)"
            )
    elif concise:
        if skipped_error_jobs:
            print(
                f"Done with {len(skipped_error_jobs)} "
                f"track{'' if len(skipped_error_jobs) == 1 else 's'} "
                "omitted due to unresolved SCSI/C2 errors."
            )
        else:
            print(
                f"Done. SCSI={errors['SCSI']}, "
                f"C2={errors['C2']}, Q={errors['Q']}"
            )

    if skipped_error_jobs:
        skipped_tracks = ", ".join(
            f"{job['track']['number']:02d}"
            for job in skipped_error_jobs
        )
        sys.exit(
            "ERROR: --abort-on-skip omitted output for "
            f"Track{'' if len(skipped_error_jobs) == 1 else 's'} "
            f"{skipped_tracks} because unresolved SCSI/C2 errors "
            "remain. Clean track files were retained."
        )
