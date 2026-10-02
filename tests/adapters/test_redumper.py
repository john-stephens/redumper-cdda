import unittest
from pathlib import Path

from redumper_cdda.adapters.redumper import (
    RedumperClient,
    RedumperCommandFactory,
    RedumperIntegrityParser,
    RedumperProcessExecutor,
    RedumperStateInspector,
)
from redumper_cdda.domain.disc import Track, TrackKind
from redumper_cdda.domain.errors import IntegrityStatusError
from redumper_cdda.domain.extraction import SectorRange
from redumper_cdda.domain.integrity import WriteOffsetMap
from redumper_cdda.ports.process import CommandResult
from tests.contracts.fakes import RecordingReporter
from types import SimpleNamespace
from unittest import mock


class RedumperCommandFactoryTests(unittest.TestCase):
    def test_exact_bounded_commands_and_split_modes(self):
        factory = RedumperCommandFactory("/dev/sg4", Path("/work"), "track02", 100)
        sector_range = SectorRange(17385, 34311)
        common = [
            "--drive=/dev/sg4", "--image-path=/work", "--image-name=track02",
            "--retries=100", "--lba-start=17385", "--lba-end=34311",
        ]
        self.assertEqual(factory.dump(sector_range), ["redumper", "dump", *common])
        self.assertEqual(factory.refine(sector_range), ["redumper", "refine", *common])
        split = [
            "redumper", "split", "--image-path=/work",
            "--image-name=track02", "--force-split",
        ]
        self.assertEqual(factory.split(), split)
        self.assertEqual(factory.split(include_data=True), split + ["--filesystem-trim"])

    def test_state_inspector_translates_failures(self):
        def fail(*_args):
            raise RuntimeError("bad state")

        inspector = RedumperStateInspector(fail)
        track = Track(1, TrackKind.AUDIO, 0, 0, 1)
        with self.assertRaisesRegex(IntegrityStatusError, "bad state"):
            inspector.inspect("state", (track,), WriteOffsetMap(((0, 0),)))

    def test_state_inspector_serializes_tracks_and_returns_typed_errors(self):
        captured = []

        def inspect(path, tracks, offsets):
            captured.append((path, tracks, offsets))
            return {1: {"SCSI": 2, "C2": 3, "SCSI sectors": 1, "C2 sectors": 1}}

        track = Track(1, TrackKind.AUDIO, 0, 0, 10, "msf", "begin")
        result = RedumperStateInspector(inspect).inspect(
            "state", (track,), WriteOffsetMap(((0, 1),))
        )
        self.assertEqual(result[1].scsi_samples, 2)
        self.assertEqual(captured[0][1][0]["length"], 10)

    def test_integrity_parser_success_and_failures(self):
        parser = RedumperIntegrityParser(
            lambda text: {"SCSI": 1, "C2": 2, "Q": 3} if text else None,
            lambda text: [(0, 1)] if text else (_ for _ in ()).throw(RuntimeError("bad")),
        )
        self.assertEqual(parser.media_errors("ok").c2, 2)
        self.assertEqual(parser.write_offsets("ok").boundaries, ((0, 1),))
        with self.assertRaises(IntegrityStatusError):
            parser.media_errors("")
        with self.assertRaisesRegex(IntegrityStatusError, "bad"):
            parser.write_offsets("")

    def test_process_executor_progress_and_client_operations(self):
        reporter = RecordingReporter()
        runner = mock.Mock()

        def streaming(command, observer):
            observer("detail\n")
            observer("[ 10%] LBA: 1\n")
            observer("[ 10%] duplicate\n")
            observer("[100%] done\n")
            return CommandResult(tuple(command), 0, "captured")

        runner.run_streaming.side_effect = streaming
        executor = RedumperProcessExecutor(runner, reporter)
        client = RedumperClient(executor)
        plan = SimpleNamespace(
            dump_command=["redumper", "dump"],
            refine_command=["redumper", "refine"],
            split_command=["redumper", "split"],
            selection=SimpleNamespace(
                tracks=(Track(1, TrackKind.AUDIO, 0, 0, 50),)
            ),
        )
        self.assertEqual(client.dump(plan).output, "captured")
        self.assertEqual(client.refine(plan).returncode, 0)
        self.assertEqual(client.split(plan).command[-1], "split")
        progress = [event for event in reporter.events if event.name == "progress"]
        self.assertEqual(progress[0].values["label"], "Reading track 01")
        self.assertEqual(progress[-1].values["label"], "Refining track 01")

    def test_client_retries_base_lba_split_failure_with_qtoc(self):
        executor = mock.Mock()
        executor.run.side_effect = (
            CommandResult(
                ("redumper", "split"),
                255,
                "error: unable to establish base LBA\n",
            ),
            CommandResult(
                ("redumper", "split", "--force-qtoc"),
                0,
                "disc write offset: +0\n",
            ),
        )
        plan = SimpleNamespace(split_command=["redumper", "split", "--force-split"])

        result = RedumperClient(executor).split(plan)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            executor.run.call_args_list,
            [
                mock.call(["redumper", "split", "--force-split"]),
                mock.call(
                    [
                        "redumper",
                        "split",
                        "--force-split",
                        "--force-qtoc",
                    ]
                ),
            ],
        )

    def test_client_does_not_retry_other_or_already_qtoc_split_failures(self):
        executor = mock.Mock()
        executor.run.return_value = CommandResult(
            ("redumper", "split"), 255, "error: unrelated\n"
        )
        client = RedumperClient(executor)
        ordinary = SimpleNamespace(
            split_command=["redumper", "split", "--force-split"]
        )

        self.assertEqual(client.split(ordinary).returncode, 255)
        executor.run.assert_called_once()

        executor.reset_mock()
        executor.run.return_value = CommandResult(
            ("redumper", "split"),
            255,
            "error: unable to establish base LBA\n",
        )
        qtoc = SimpleNamespace(
            split_command=[
                "redumper", "split", "--force-split", "--force-qtoc"
            ]
        )

        self.assertEqual(client.split(qtoc).returncode, 255)
        executor.run.assert_called_once()

    def test_progress_track_fallbacks_and_data_label(self):
        audio = Track(1, TrackKind.AUDIO, 0, 10, 20)
        data = Track(2, TrackKind.DATA, 4, 20, 30)
        self.assertIs(
            RedumperProcessExecutor._track_for_lba((audio, data), 0), audio
        )
        self.assertIs(
            RedumperProcessExecutor._track_for_lba((audio, data), 99), data
        )
        self.assertIsNone(RedumperProcessExecutor._track_for_lba((), 1))
        reporter = RecordingReporter()
        runner = mock.Mock()

        def streaming(command, observer):
            observer("[ 50%] LBA: 25\n")
            return CommandResult(tuple(command), 0, "")

        runner.run_streaming.side_effect = streaming
        RedumperProcessExecutor(runner, reporter).run(
            ["redumper", "dump"], progress_tracks=(audio, data)
        )
        progress = next(event for event in reporter.events if event.name == "progress")
        self.assertEqual(progress.values["label"], "Reading data track 02")

    def test_process_executor_without_progress(self):
        reporter = RecordingReporter()
        runner = mock.Mock()

        def streaming(command, observer):
            observer("no percentage\n")
            return CommandResult(tuple(command), 0, "text")

        runner.run_streaming.side_effect = streaming
        RedumperProcessExecutor(runner, reporter).run(["redumper", "split"])
        self.assertNotIn("progress_end", [event.name for event in reporter.events])

        reporter.events.clear()

        def percentage(command, observer):
            observer("[ 25%] no lba\n")
            return CommandResult(tuple(command), 0, "text")

        runner.run_streaming.side_effect = percentage
        RedumperProcessExecutor(runner, reporter).run(["redumper", "dump"])
        progress = next(event for event in reporter.events if event.name == "progress")
        self.assertEqual(progress.values["label"], "Reading")
