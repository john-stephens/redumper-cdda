#!/usr/bin/env python3
"""Command-line composition and error boundary."""

import argparse
from contextlib import ExitStack
from functools import partial
import re
import shutil
import signal
import sys
from pathlib import Path

from .adapters.console import MultiplexReporter, VerboseReporter, reporter_for
from .bootstrap import create_application
from .domain.errors import (
    DependencyError,
    OutputError,
    RedumperCddaError,
    TerminationRequested,
)
from .domain.events import LifecycleEvent
from .domain.extraction import ExtractionRequest, TrackSelection


DEFAULT_REFINE_PASSES = 3


class ArgumentParserFactory:
    def create(self):
        parser = argparse.ArgumentParser(
            description=(
                "Extract one or more CD tracks using MMC and cdparanoia for "
                "validated boundaries and redumper for extraction."
            ),
        )
        parser.add_argument(
            "device",
            nargs="?",
            help=(
                "SCSI generic optical-drive path, such as /dev/sg4; optional "
                "with --existing-dump and --cdparanoia-toc-file"
            ),
        )
        parser.add_argument(
            "track", type=self.track_selection, nargs="?",
            default=TrackSelection(), metavar="TRACK",
            help=(
                "track selection: N, N-M, -M, N-, or - for the full disc "
                "(default: full disc)"
            ),
        )
        parser.add_argument(
            "--output", type=Path,
            help="output path for --single-file mode",
        )
        parser.add_argument(
            "--retries", type=int, default=100,
            help="redumper retries within each dump or refine pass (default: 100)",
        )
        parser.add_argument(
            "--refine-passes",
            type=self.refine_pass_count,
            default=DEFAULT_REFINE_PASSES,
            metavar="N",
            help="maximum refine passes; 0 means unlimited (default: 3)",
        )
        parser.add_argument(
            "--refine-forever",
            dest="refine_passes",
            action="store_const",
            const=None,
            help="alias for --refine-passes=0",
        )
        parser.add_argument(
            "--log-file",
            type=Path,
            metavar="PATH",
            help="write complete verbose diagnostics to PATH",
        )
        parser.add_argument(
            "--existing-dump",
            type=Path,
            metavar="PATH",
            help=(
                "use an existing redumper dump, specified as its path without "
                "an extension; copy it to the temporary workspace and skip "
                "dump/refine"
            ),
        )
        parser.add_argument(
            "--cdparanoia-toc-file",
            type=Path,
            metavar="PATH",
            help="read captured cdparanoia -Q text output from PATH",
        )
        parser.add_argument(
            "-X", "--abort-on-skip", action="store_true",
            help="omit outputs containing unresolved SCSI/C2 errors",
        )
        parser.add_argument(
            "-s", "--single-file", action="store_true",
            help="combine selected audio tracks into one WAV",
        )
        parser.add_argument(
            "-p", "--prefix", default="track", metavar="PREFIX",
            help="prefix for automatically named output files (default: track)",
        )
        parser.add_argument(
            "-d", "--include-data", action="store_true",
            help="include data tracks and write them as validated ISO files",
        )
        parser.add_argument(
            "--show-layout", action="store_true",
            help="show the complete audio/data track layout and exit",
        )
        parser.add_argument(
            "--no-accuraterip", dest="accuraterip", action="store_false", default=True,
            help="disable AccurateRip verification",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "-v", "--verbose", action="store_true",
            help="show commands, complete tool output, and diagnostics",
        )
        mode.add_argument(
            "-q", "--quiet", action="store_true",
            help="suppress routine terminal output",
        )
        return parser

    @staticmethod
    def track_selection(value):
        if value == "-":
            return TrackSelection()
        if re.fullmatch(r"\d+", value):
            number = int(value)
            return TrackSelection(number, number)
        match = re.fullmatch(r"(\d*)-(\d*)", value)
        if not match:
            raise argparse.ArgumentTypeError("track must be N, N-M, -M, N-, or -")
        return TrackSelection(
            int(match.group(1)) if match.group(1) else None,
            int(match.group(2)) if match.group(2) else None,
        )

    @staticmethod
    def refine_pass_count(value):
        try:
            count = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "refine passes must be a non-negative integer"
            ) from exc
        if count < 0:
            raise argparse.ArgumentTypeError(
                "refine passes must be a non-negative integer"
            )
        return None if count == 0 else count


class SystemDependencyChecker:
    def __init__(self, which=shutil.which):
        self._which = which

    def check(
        self,
        show_layout=False,
        mmc_from_files=False,
        cdparanoia_from_file=False,
    ):
        required = []
        if not cdparanoia_from_file:
            required.append("cdparanoia")
        if not mmc_from_files:
            required.append("sg_raw")
        if not show_layout:
            required.append("redumper")
        for executable in required:
            if self._which(executable) is None:
                raise DependencyError(f"{executable} not found")


class CliApplication:
    def __init__(
        self,
        parser_factory=ArgumentParserFactory,
        dependency_checker=None,
        application_factory=create_application,
        reporter_factory=reporter_for,
    ):
        self._parser_factory = parser_factory
        self._dependency_checker = dependency_checker or SystemDependencyChecker()
        self._application_factory = application_factory
        self._reporter_factory = reporter_factory

    def execute(self, argv=None):
        parser = self._parser_factory().create()
        args = parser.parse_intermixed_args(argv)
        self._resolve_offline_positionals(args)
        self._validate(parser, args)
        self._dependency_checker.check(
            args.show_layout,
            mmc_from_files=args.existing_dump is not None,
            cdparanoia_from_file=args.cdparanoia_toc_file is not None,
        )
        with ExitStack() as stack:
            reporter = self._reporter_factory(args.verbose, args.quiet)
            if args.log_file is not None:
                try:
                    stream = stack.enter_context(
                        args.log_file.open("w", encoding="utf-8")
                    )
                except OSError as exc:
                    raise OutputError(
                        f"Could not open log file {args.log_file}: {exc}"
                    ) from exc
                reporter = MultiplexReporter(
                    reporter,
                    VerboseReporter(partial(print, file=stream)),
                )
            application = self._application_factory(
                reporter,
                existing_dump=args.existing_dump,
                cdparanoia_toc_file=args.cdparanoia_toc_file,
            )
            if args.show_layout:
                layout = application.read_layout(args.device or "-")
                reporter.publish(
                    LifecycleEvent("disc_layout", layout)
                )
                return 0
            request = ExtractionRequest(
                device=args.device or "-",
                selection=args.track,
                include_data=args.include_data,
                single_file=args.single_file,
                output=args.output,
                prefix=args.prefix,
                retries=args.retries,
                refine_passes=args.refine_passes,
                abort_on_skip=args.abort_on_skip,
                accuraterip=args.accuraterip,
                existing_dump=args.existing_dump,
            )
            application.run(request)
            return 0

    @staticmethod
    def _resolve_offline_positionals(args):
        if not (
            args.existing_dump is not None
            and args.cdparanoia_toc_file is not None
            and args.device is not None
            and args.track == TrackSelection()
        ):
            return
        try:
            selection = ArgumentParserFactory.track_selection(args.device)
        except argparse.ArgumentTypeError:
            return
        args.device = None
        args.track = selection

    @staticmethod
    def _validate(parser, args):
        if args.device in (None, "-") and not (
            args.existing_dump is not None
            and args.cdparanoia_toc_file is not None
        ):
            parser.error(
                "device is required unless --existing-dump and "
                "--cdparanoia-toc-file are supplied"
            )
        if args.retries < 0:
            parser.error("invalid retry count")
        if (
            not args.prefix
            or args.prefix in (".", "..")
            or Path(args.prefix).name != args.prefix
        ):
            parser.error(
                "--prefix must be a non-empty filename prefix without directory components"
            )
        if not args.show_layout and not args.single_file and args.output:
            parser.error("--output requires --single-file")
        if not args.show_layout and args.include_data and args.single_file and (
            args.track.start is None
            or args.track.end is None
            or args.track.start != args.track.end
        ):
            parser.error("--include-data cannot use --single-file for a track range")


def handle_termination_signal(signum, _frame):
    raise TerminationRequested(signum, signal.Signals(signum).name)


def install_signal_handlers():
    for name in ("SIGHUP", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, handle_termination_signal)


def main(argv=None):
    return CliApplication().execute(argv)


def run():
    install_signal_handlers()
    try:
        code = main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Temporary files were cleaned up.")
    except TerminationRequested as exc:
        sys.exit(
            f"\nReceived {exc.signal_name}. Temporary files were cleaned up."
        )
    except RedumperCddaError as exc:
        sys.exit(f"ERROR: {exc}")
    if code:
        sys.exit(code)


if __name__ == "__main__":
    run()
