#!/usr/bin/env python3
"""Command-line composition and error boundary."""

import argparse
import re
import shutil
import signal
import sys
from pathlib import Path

from .adapters.console import reporter_for
from .bootstrap import create_application
from .domain.errors import DependencyError, RedumperCddaError, TerminationRequested
from .domain.events import LifecycleEvent
from .domain.extraction import ExtractionRequest, TrackSelection


DEFAULT_REFINE_PASSES = 3


class ArgumentParserFactory:
    def create(self):
        parser = argparse.ArgumentParser(
            description=(
                "Extract one or more CD tracks using MMC and cdparanoia for "
                "validated boundaries and redumper for extraction."
            )
        )
        parser.add_argument("device")
        parser.add_argument(
            "track", type=self.track_selection, nargs="?",
            default=TrackSelection(), metavar="TRACK",
        )
        parser.add_argument("--output", type=Path)
        parser.add_argument("--retries", type=int, default=100)
        parser.add_argument("--refine-passes", type=int, default=DEFAULT_REFINE_PASSES)
        parser.add_argument("-X", "--abort-on-skip", action="store_true")
        parser.add_argument("-s", "--single-file", action="store_true")
        parser.add_argument("-p", "--prefix", default="track", metavar="PREFIX")
        parser.add_argument("-d", "--include-data", action="store_true")
        parser.add_argument("--show-layout", action="store_true")
        parser.add_argument(
            "--no-accuraterip", dest="accuraterip", action="store_false", default=True
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("-v", "--verbose", action="store_true")
        mode.add_argument("-q", "--quiet", action="store_true")
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


class SystemDependencyChecker:
    def __init__(self, which=shutil.which):
        self._which = which

    def check(self, show_layout=False):
        required = ["cdparanoia", "sg_raw"]
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
        args = parser.parse_args(argv)
        self._validate(parser, args)
        self._dependency_checker.check(args.show_layout)
        reporter = self._reporter_factory(args.verbose, args.quiet)
        application = self._application_factory(reporter)
        if args.show_layout:
            reporter.publish(
                LifecycleEvent("disc_layout", application.read_layout(args.device))
            )
            return 0
        request = ExtractionRequest(
            device=args.device,
            selection=args.track,
            include_data=args.include_data,
            single_file=args.single_file,
            output=args.output,
            prefix=args.prefix,
            retries=args.retries,
            refine_passes=args.refine_passes,
            abort_on_skip=args.abort_on_skip,
            accuraterip=args.accuraterip,
        )
        application.run(request)
        return 0

    @staticmethod
    def _validate(parser, args):
        if args.retries < 0:
            parser.error("invalid retry count")
        if args.refine_passes < 0:
            parser.error("invalid refine-pass count")
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
