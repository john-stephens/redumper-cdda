#!/usr/bin/env python3

import argparse
import signal
import shutil
import sys
import tempfile
from pathlib import Path

from .accuraterip import load_accuraterip_library
from .layout import parse_track_selection
from .workflow import (
    TerminationRequested,
    extract_track,
    read_disc_layout,
)


DEFAULT_REFINE_PASSES = 3


def print_disc_layout(tracks):
    print()
    print("Disc track layout")
    print("-----------------")

    print(
        f"{'Track':>5}  "
        f"{'Type':<5}  "
        f"{'Length':>10}  "
        f"{'Begin':>10}  "
        f"{'End':>10}"
    )

    for track in tracks:
        print(
            f"{track['number']:>5}  "
            f"{track.get('kind', 'audio'):<5}  "
            f"{track['length']:>10}  "
            f"{track['begin']:>10}  "
            f"{track['end']:>10}"
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Extract one or more CD tracks using MMC and cdparanoia for\n"
            "validated track boundaries and redumper for the actual\n"
            "offset-corrected extraction."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
TRACK SELECTION:
  N      extract Track N
  N-M    extract Tracks N through M, inclusive
  -M     extract Track 1 through M, inclusive
  N-     extract Track N through the final numbered track
  -      extract Track 1 through the final numbered track (default)

Omitting TRACK is equivalent to '-'. Track 0 is never implicit; request it
explicitly with 0 or a range such as 0-3.

By default, open ranges omit data tracks. A single N or fully bounded N-M
selection fails if it explicitly names a data track. Use --include-data to
include data tracks. --single-file cannot combine audio and data tracks.
""",
    )

    parser.add_argument(
        "device",
    )

    parser.add_argument(
        "track",
        type=parse_track_selection,
        nargs="?",
        default=parse_track_selection("-"),
        metavar="TRACK",
        help=(
            "Track number or range: N, N-M, -M, N-, or - "
            "(default: -, the full disc)"
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Output path for --single-file "
            "(default: ./PREFIX.wav or ./PREFIXNN.ext)"
        ),
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=100,
        help=(
            "Sector retries per redumper pass "
            "(default: 100)"
        ),
    )

    parser.add_argument(
        "--refine-passes",
        type=int,
        default=DEFAULT_REFINE_PASSES,
        help=(
            "Maximum refine passes when SCSI/C2 "
            f"errors remain (default: "
            f"{DEFAULT_REFINE_PASSES})"
        ),
    )

    parser.add_argument(
        "-X",
        "--abort-on-skip",
        action="store_true",
        help=(
            "Skip separate track files with unresolved SCSI/C2 "
            "errors; --single-file remains all-or-nothing "
            "(default: write affected output with a warning)"
        ),
    )

    parser.add_argument(
        "-s",
        "--single-file",
        action="store_true",
        help=(
            "Combine selected audio tracks into one PREFIX.wav "
            "(default: one PREFIXNN.wav or PREFIXNN.iso per track)"
        ),
    )

    parser.add_argument(
        "-p",
        "--prefix",
        default="track",
        metavar="PREFIX",
        help=(
            "Prefix for automatically named output files "
            "(default: track)"
        ),
    )

    parser.add_argument(
        "-d",
        "--include-data",
        action="store_true",
        help=(
            "Include data tracks as PREFIXNN.iso; incompatible with "
            "--single-file unless TRACK is one explicit data track "
            "(default: omit data tracks)"
        ),
    )

    parser.add_argument(
        "--show-layout",
        action="store_true",
        help=(
            "Print the complete audio/data track layout and exit "
            "without dumping"
        ),
    )

    parser.add_argument(
        "--no-accuraterip",
        dest="accuraterip",
        action="store_false",
        default=True,
        help=(
            "Disable AccurateRip verification "
            "(default: enabled)"
        ),
    )

    output_mode = parser.add_mutually_exclusive_group()

    output_mode.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Show commands, full tool output, ranges, "
            "and conversion details "
            "(default output mode: concise)"
        ),
    )

    output_mode.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help=(
            "Suppress routine output "
            "(default output mode: concise)"
        ),
    )

    args = parser.parse_args()

    if args.retries < 0:
        sys.exit(
            "ERROR: invalid retry count"
        )

    if args.refine_passes < 0:
        sys.exit(
            "ERROR: invalid refine-pass count"
        )

    if (
        not args.prefix
        or args.prefix in (".", "..")
        or Path(args.prefix).name != args.prefix
    ):
        parser.error(
            "--prefix must be a non-empty filename prefix "
            "without directory components"
        )

    if args.accuraterip and not args.show_layout:
        try:
            load_accuraterip_library()
        except RuntimeError:
            args.accuraterip = False

    if (
        args.include_data
        and not args.show_layout
        and args.single_file
        and (
            args.track["start"] is None
            or args.track["end"] is None
            or args.track["start"]
            != args.track["end"]
        )
    ):
        parser.error(
            "--include-data cannot use --single-file for a track range"
        )

    if shutil.which(
        "cdparanoia"
    ) is None:
        sys.exit(
            "ERROR: cdparanoia not found"
        )

    if shutil.which(
        "sg_raw"
    ) is None:
        sys.exit(
            "ERROR: sg_raw not found"
        )

    if args.show_layout:
        try:
            tracks = read_disc_layout(
                args.device,
                verbose=args.verbose,
            )
        except RuntimeError as exc:
            sys.exit(
                f"ERROR: {exc}"
            )

        if not args.quiet:
            print_disc_layout(
                tracks
            )
        return

    if not args.single_file and args.output:
        parser.error(
            "--output requires --single-file; separate files use "
            "PREFIXNN.wav or PREFIXNN.iso in the current directory"
        )

    if shutil.which(
        "redumper"
    ) is None:
        sys.exit(
            "ERROR: redumper not found"
        )

    prefix = "redumper-cdda-"

    with tempfile.TemporaryDirectory(
        prefix=prefix,
    ) as temporary_path:
        workdir = Path(temporary_path)

        if args.verbose:
            print(
                f"Temporary workspace: {workdir}"
            )

        extract_track(
            args,
            workdir,
        )

    if args.verbose:
        print(
            f"Temporary workspace removed: {workdir}"
        )


def handle_termination_signal(signum, _frame):
    raise TerminationRequested(signum)


def install_signal_handlers():
    for signal_name in (
        "SIGHUP",
        "SIGTERM",
    ):
        signum = getattr(
            signal,
            signal_name,
            None,
        )

        if signum is not None:
            signal.signal(
                signum,
                handle_termination_signal,
            )


def run():
    install_signal_handlers()

    try:
        main()
    except KeyboardInterrupt:
        sys.exit(
            "\nInterrupted. Temporary files were cleaned up."
        )
    except TerminationRequested as exc:
        sys.exit(
            f"\nReceived {exc.signal_name}. "
            "Temporary files were cleaned up."
        )


if __name__ == "__main__":
    run()
