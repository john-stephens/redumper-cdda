"""Tests for the command-line composition and error boundary."""

import argparse
import importlib
import io
import signal
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from redumper_cdda import cli
from redumper_cdda.domain.disc import DiscLayout, Track, TrackKind
from redumper_cdda.domain.errors import DependencyError, LayoutError
from redumper_cdda.domain.extraction import TrackSelection


class ArgumentParserFactoryTests(unittest.TestCase):
    def test_all_track_selection_forms(self):
        parse = cli.ArgumentParserFactory.track_selection
        self.assertEqual(parse("-"), TrackSelection())
        self.assertEqual(parse("2"), TrackSelection(2, 2))
        self.assertEqual(parse("1-3"), TrackSelection(1, 3))
        self.assertEqual(parse("-3"), TrackSelection(None, 3))
        self.assertEqual(parse("3-"), TrackSelection(3, None))
        with self.assertRaises(argparse.ArgumentTypeError):
            parse("bad")
        with self.assertRaises(argparse.ArgumentTypeError):
            cli.ArgumentParserFactory.refine_pass_count("bad")

    def test_parser_defaults_and_options(self):
        args = cli.ArgumentParserFactory().create().parse_args(
            ["/dev/sg4", "2", "-s", "--output", "x.wav", "-p", "album",
             "--retries", "5", "--refine-passes", "2", "--refine-forever",
             "--log-file", "rip.log", "--existing-dump", "/dumps/disc",
             "--cdparanoia-toc-file", "cdparanoia.txt",
             "-X", "--no-accuraterip"]
        )
        self.assertEqual(args.track, TrackSelection(2, 2))
        self.assertEqual(args.output, Path("x.wav"))
        self.assertFalse(args.accuraterip)
        self.assertIsNone(args.refine_passes)
        self.assertEqual(args.log_file, Path("rip.log"))
        self.assertEqual(args.existing_dump, Path("/dumps/disc"))
        self.assertEqual(args.cdparanoia_toc_file, Path("cdparanoia.txt"))

        unlimited = cli.ArgumentParserFactory().create().parse_args(
            ["/dev/sg4", "--refine-passes=0"]
        )
        self.assertIsNone(unlimited.refine_passes)

        help_text = cli.ArgumentParserFactory().create().format_help()
        for option in (
            "--output", "--retries", "--refine-passes", "--refine-forever",
            "--log-file", "--abort-on-skip", "--single-file", "--prefix",
            "--include-data", "--show-layout", "--no-accuraterip",
            "--existing-dump", "--verbose", "--quiet",
            "--cdparanoia-toc-file",
        ):
            self.assertIn(option, help_text)
        self.assertIn("0 means unlimited", help_text)


class SystemDependencyCheckerTests(unittest.TestCase):
    def test_checks_only_required_tools(self):
        calls = []
        System = cli.SystemDependencyChecker(lambda name: calls.append(name) or name)
        System.check(show_layout=True)
        self.assertEqual(calls, ["cdparanoia", "sg_raw"])
        System.check(show_layout=False)
        self.assertEqual(calls[-3:], ["cdparanoia", "sg_raw", "redumper"])
        calls.clear()
        System.check(
            show_layout=True,
            mmc_from_files=True,
            cdparanoia_from_file=True,
        )
        self.assertEqual(calls, [])
        System.check(
            show_layout=False,
            mmc_from_files=True,
            cdparanoia_from_file=True,
        )
        self.assertEqual(calls, ["redumper"])

    def test_missing_tool_is_typed_error(self):
        with self.assertRaisesRegex(DependencyError, "sg_raw"):
            cli.SystemDependencyChecker(
                lambda name: None if name == "sg_raw" else name
            ).check()


class CliApplicationTests(unittest.TestCase):
    def make_cli(self):
        reporter = mock.Mock()
        application = mock.Mock()
        checker = mock.Mock()
        command = cli.CliApplication(
            dependency_checker=checker,
            application_factory=lambda _reporter, **_options: application,
            reporter_factory=lambda _verbose, _quiet: reporter,
        )
        return command, checker, application, reporter

    def test_extract_builds_typed_request(self):
        command, checker, application, _reporter = self.make_cli()
        self.assertEqual(
            command.execute(["/dev/sg4", "2", "--no-accuraterip", "-q"]), 0
        )
        request = application.run.call_args.args[0]
        self.assertEqual(request.selection, TrackSelection(2, 2))
        self.assertFalse(request.accuraterip)
        self.assertEqual(request.refine_passes, cli.DEFAULT_REFINE_PASSES)
        self.assertIsNone(request.existing_dump)
        checker.check.assert_called_once_with(
            False,
            mmc_from_files=False,
            cdparanoia_from_file=False,
        )

    def test_log_file_receives_verbose_events_with_concise_console(self):
        checker = mock.Mock()

        class Application:
            def __init__(self, reporter, **_options):
                self.reporter = reporter

            def run(self, request):
                self.reporter.publish(
                    cli.LifecycleEvent("command_started", ("redumper", "dump"))
                )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rip.log"
            command = cli.CliApplication(
                dependency_checker=checker,
                application_factory=Application,
                reporter_factory=lambda _verbose, _quiet: cli.reporter_for(
                    output=mock.Mock()
                ),
            )

            self.assertEqual(
                command.execute(["drive", "1", "--log-file", str(path)]), 0
            )

            self.assertIn("+ redumper dump", path.read_text(encoding="utf-8"))

    def test_log_file_open_failure_is_typed(self):
        command, _checker, _application, _reporter = self.make_cli()
        with self.assertRaisesRegex(cli.OutputError, "Could not open log file"):
            command.execute(["drive", "--log-file", "/missing/dir/rip.log"])

    def test_show_layout_reports_without_extracting(self):
        command, checker, application, reporter = self.make_cli()
        track = Track(1, TrackKind.AUDIO, 0, 0, 10)
        application.read_layout.return_value = DiscLayout((track,), 10)
        self.assertEqual(command.execute(["drive", "--show-layout"]), 0)
        self.assertEqual(reporter.publish.call_args.args[0].name, "disc_layout")
        application.run.assert_not_called()
        checker.check.assert_called_once_with(
            True,
            mmc_from_files=False,
            cdparanoia_from_file=False,
        )

    def test_show_layout_ignores_extraction_only_output_validation(self):
        command, _checker, application, _reporter = self.make_cli()
        track = Track(1, TrackKind.AUDIO, 0, 0, 10)
        application.read_layout.return_value = DiscLayout((track,), 10)
        self.assertEqual(
            command.execute(
                ["drive", "--show-layout", "--output", "x.wav", "-d", "-s", "1-2"]
            ),
            0,
        )

    def test_cli_only_validation(self):
        invalid = (
            ([], "2"),
            (["drive", "--retries", "-1"], "retry"),
            (["drive", "--refine-passes", "-1"], "2"),
            (["drive", "--prefix", "../x"], "prefix"),
            (["drive", "--output", "x.wav"], "single-file"),
            (["drive", "-d", "-s", "1-2"], "track range"),
            (["drive", "--mmc-toc-file", "disc.toc"], "2"),
            (["-", "--show-layout"], "2"),
            (
                [
                    "-", "--show-layout",
                    "--cdparanoia-toc-file", "cd.txt",
                ],
                "2",
            ),
        )
        for arguments, message in invalid:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaisesRegex(SystemExit, "2"):
                cli.CliApplication().execute(arguments)

    def test_offline_layout_files_are_passed_to_composition(self):
        checker = mock.Mock()
        application = mock.Mock()
        application.read_layout.return_value = DiscLayout(
            (Track(1, TrackKind.AUDIO, 0, 0, 10),), 10
        )
        factory = mock.Mock(return_value=application)
        command = cli.CliApplication(
            dependency_checker=checker,
            application_factory=factory,
            reporter_factory=lambda _verbose, _quiet: mock.Mock(),
        )

        self.assertEqual(
            command.execute(
                [
                    "--show-layout",
                    "--existing-dump", "dump/disc",
                    "--cdparanoia-toc-file", "cd.txt",
                ]
            ),
            0,
        )
        checker.check.assert_called_once_with(
            True, mmc_from_files=True, cdparanoia_from_file=True
        )
        factory.assert_called_once_with(
            mock.ANY,
            existing_dump=Path("dump/disc"),
            cdparanoia_toc_file=Path("cd.txt"),
        )
        application.read_layout.assert_called_once_with("-")

    def test_offline_extraction_uses_existing_dump_without_drive_tools(self):
        command, checker, application, _reporter = self.make_cli()
        arguments = [
            "2", "--existing-dump", "dump/disc",
            "--cdparanoia-toc-file", "cd.txt",
        ]

        self.assertEqual(command.execute(arguments), 0)

        checker.check.assert_called_once_with(
            False, mmc_from_files=True, cdparanoia_from_file=True
        )
        request = application.run.call_args.args[0]
        self.assertEqual(request.device, "-")
        self.assertEqual(request.selection, TrackSelection(2, 2))
        self.assertEqual(request.existing_dump, Path("dump/disc"))

    def test_offline_sources_can_still_use_an_explicit_device(self):
        command, _checker, application, _reporter = self.make_cli()

        self.assertEqual(
            command.execute(
                [
                    "/dev/sg4",
                    "--existing-dump", "dump/disc",
                    "--cdparanoia-toc-file", "cd.txt",
                ]
            ),
            0,
        )

        request = application.run.call_args.args[0]
        self.assertEqual(request.device, "/dev/sg4")
        self.assertEqual(request.selection, TrackSelection())


class CliBoundaryTests(unittest.TestCase):
    def test_signal_handler_and_installation(self):
        with self.assertRaises(cli.TerminationRequested) as caught:
            cli.handle_termination_signal(signal.SIGTERM, None)
        self.assertEqual(caught.exception.signal_name, "SIGTERM")
        with mock.patch.object(cli.signal, "signal") as register:
            cli.install_signal_handlers()
        self.assertTrue(register.called)
        with mock.patch.object(cli.signal, "SIGHUP", None), mock.patch.object(cli.signal, "signal") as register:
            cli.install_signal_handlers()
        self.assertEqual(register.call_count, 1)

    def test_run_translates_interrupt_termination_and_application_errors(self):
        cases = (
            (KeyboardInterrupt(), "Interrupted"),
            (cli.TerminationRequested(signal.SIGTERM, "SIGTERM"), "SIGTERM"),
            (LayoutError("bad layout"), "ERROR: bad layout"),
        )
        for exception, message in cases:
            with (
                self.subTest(exception=exception),
                mock.patch.object(cli, "install_signal_handlers"),
                mock.patch.object(cli, "main", side_effect=exception),
                self.assertRaisesRegex(SystemExit, message),
            ):
                cli.run()

    def test_run_success_and_nonzero_code(self):
        with mock.patch.object(cli, "install_signal_handlers"), mock.patch.object(cli, "main", return_value=0):
            cli.run()

    def test_main_and_module_entry_points(self):
        with mock.patch.object(cli.CliApplication, "execute", return_value=0) as execute:
            self.assertEqual(cli.main(["drive"]), 0)
        execute.assert_called_once_with(["drive"])
        importlib.import_module("redumper_cdda.__main__")
        with mock.patch.object(sys, "argv", ["redumper-cdda"]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runpy.run_module("redumper_cdda.cli", run_name="__main__")
        with mock.patch.object(cli, "run") as run:
            runpy.run_module("redumper_cdda.__main__", run_name="__main__")
        run.assert_called_once_with()
        with mock.patch.object(cli, "install_signal_handlers"), mock.patch.object(cli, "main", return_value=3), self.assertRaisesRegex(SystemExit, "3"):
            cli.run()


if __name__ == "__main__":
    unittest.main()
