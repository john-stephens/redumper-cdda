import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from redumper_cdda.adapters.redumper import (
    RedumperClient,
    RedumperCommandFactory,
    RedumperIntegrityParser,
    RedumperProcessExecutor,
    RedumperStateInspector,
)
from redumper_cdda.domain.disc import DiscLayout, Track, TrackKind
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
        self.assertEqual(factory.split(), split + ["--force-offset=0"])
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
            observer("[ 10%] LBA: 1, errors: { SCSIs: 0, C2s: 2, Q: 1 }\n")
            observer("[ 10%] duplicate\n")
            observer("[100%] LBA: 49, errors: { SCSIs: 0, C2s: 2, Q: 1 }\n")
            return CommandResult(tuple(command), 0, "captured")

        runner.run_streaming.side_effect = streaming
        executor = RedumperProcessExecutor(runner, reporter)
        client = RedumperClient(executor)
        plan = SimpleNamespace(
            dump_command=["redumper", "dump"],
            refine_command=["redumper", "refine"],
            split_command=["redumper", "split"],
            workdir=Path("/missing"),
            image_name="partial",
            disc=DiscLayout((Track(1, TrackKind.AUDIO, 0, 0, 50),), 50),
            logical_end_lba=50,
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
        self.assertEqual(progress[0].values["percent"], 4)
        self.assertEqual(progress[0].values["c2"], 2)

    def test_client_splits_partial_range_with_bounded_toc_and_restores_metadata(self):
        with TemporaryDirectory() as directory:
            workdir = Path(directory)
            toc_path = workdir / "partial.toc"
            fulltoc_path = workdir / "partial.fulltoc"
            original_toc = self._toc(
                ((1, 0, 0), (2, 0, 100), (3, 4, 200)), 300
            )
            toc_path.write_bytes(original_toc)
            fulltoc_path.write_bytes(b"full toc")
            executor = mock.Mock()

            def execute(command):
                bounded = toc_path.read_bytes()
                self.assertFalse(fulltoc_path.exists())
                self.assertEqual((bounded[2], bounded[3]), (1, 2))
                descriptors = [
                    bounded[offset:offset + 8]
                    for offset in range(4, len(bounded), 8)
                ]
                self.assertEqual([item[2] for item in descriptors], [1, 2, 0xAA])
                self.assertEqual(int.from_bytes(descriptors[-1][4:8], "big"), 200)
                return CommandResult(tuple(command), 0, "split")

            executor.run.side_effect = execute
            tracks = (
                Track(1, TrackKind.AUDIO, 0, 0, 100),
                Track(2, TrackKind.AUDIO, 0, 100, 200),
                Track(3, TrackKind.DATA, 4, 200, 300),
            )
            plan = SimpleNamespace(
                split_command=["redumper", "split", "--force-split"],
                workdir=workdir,
                image_name="partial",
                disc=DiscLayout(tracks, 300),
                selection=SimpleNamespace(first_track=tracks[0]),
                logical_start_lba=0,
                logical_end_lba=200,
            )

            result = RedumperClient(executor).split(plan)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(toc_path.read_bytes(), original_toc)
            self.assertEqual(fulltoc_path.read_bytes(), b"full toc")

            fulltoc_path.unlink()
            result = RedumperClient(executor).split(plan)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(toc_path.read_bytes(), original_toc)
            self.assertFalse(fulltoc_path.exists())
        self.assertEqual(executor.run.call_count, 2)
        executor.run.assert_called_with(["redumper", "split", "--force-split"])

    def test_client_uses_original_toc_for_complete_or_missing_metadata(self):
        executor = mock.Mock()
        executor.run.return_value = CommandResult((), 0, "split")
        track = Track(1, TrackKind.AUDIO, 0, 0, 100)
        plan = SimpleNamespace(
            split_command=["redumper", "split"],
            workdir=Path("/missing"),
            image_name="partial",
            disc=DiscLayout((track,), 100),
            logical_end_lba=100,
        )
        client = RedumperClient(executor)
        client.split(plan)
        plan.logical_end_lba = 50
        client.split(plan)
        self.assertEqual(executor.run.call_count, 2)

    def test_bounded_toc_track_zero_and_invalid_metadata_fallbacks(self):
        track = Track(1, TrackKind.AUDIO, 0, 100, 200)
        plan = SimpleNamespace(
            disc=DiscLayout((track,), 200),
            selection=SimpleNamespace(first_track=SimpleNamespace(number=0)),
            logical_start_lba=0,
            logical_end_lba=100,
        )
        bounded = RedumperClient._bounded_toc(
            plan, self._toc(((1, 0, 100),), 200)
        )
        self.assertEqual((bounded[2], bounded[3]), (1, 1))
        self.assertEqual(int.from_bytes(bounded[-4:], "big"), 100)
        for invalid in (b"bad", b"\x00\x20\x01\x01", b"\x00\x0b\x01\x01" + b"x" * 9):
            self.assertEqual(RedumperClient._bounded_toc(plan, invalid), invalid)
        no_leadout = self._toc(((1, 0, 100),), 200)[:-8]
        no_leadout = (len(no_leadout) - 2).to_bytes(2, "big") + no_leadout[2:]
        self.assertEqual(RedumperClient._bounded_toc(plan, no_leadout), no_leadout)

    @staticmethod
    def _toc(tracks, leadout):
        descriptors = []
        for number, control, lba in tracks:
            descriptors.append(
                bytes((0, 0x10 | control, number, 0)) + lba.to_bytes(4, "big")
            )
        descriptors.append(bytes((0, 0x10, 0xAA, 0)) + leadout.to_bytes(4, "big"))
        payload = b"".join(descriptors)
        return (
            (2 + len(payload)).to_bytes(2, "big")
            + bytes((tracks[0][0], tracks[-1][0]))
            + payload
        )

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
            observer("[ 50%] LBA: 25, errors: { SCSI: 1, C2: 2, Q: 3 }\n")
            return CommandResult(tuple(command), 0, "")

        runner.run_streaming.side_effect = streaming
        RedumperProcessExecutor(runner, reporter).run(
            ["redumper", "dump"], progress_tracks=(audio, data)
        )
        progress = next(event for event in reporter.events if event.name == "progress")
        self.assertEqual(progress.values["label"], "Reading data track 02")
        self.assertEqual(progress.values["percent"], 60)
        self.assertEqual(progress.values["scsi"], 1)

    def test_process_executor_ends_each_track_and_updates_refine_errors(self):
        reporter = RecordingReporter()
        runner = mock.Mock()
        audio = Track(1, TrackKind.AUDIO, 0, 10, 20)
        data = Track(2, TrackKind.DATA, 4, 20, 30)

        def dump(command, observer):
            observer("[ 25%] LBA: 14, errors: { SCSIs: 1, C2s: 4, Q: 2 }\n")
            observer("[ 75%] LBA: 24, errors: { SCSIs: 3, C2s: 7, Q: 3 }\n")
            return CommandResult(tuple(command), 0, "")

        def refine(command, observer):
            observer("[ 25%] LBA: 14, errors: { SCSIs: 2, C2s: 5, Q: 3 }\n")
            observer("[ 75%] LBA: 24, errors: { SCSIs: 1, C2s: 2, Q: 2 }\n")
            return CommandResult(tuple(command), 0, "")

        runner.run_streaming.side_effect = (
            lambda command, observer: (
                dump(command, observer)
                if command[1] == "dump"
                else refine(command, observer)
            )
        )
        executor = RedumperProcessExecutor(runner, reporter)
        executor.run(["redumper", "dump"], progress_tracks=(audio, data))
        executor.run(["redumper", "refine"], progress_tracks=(audio, data))

        progress = [event.values for event in reporter.events if event.name == "progress"]
        ends = [event for event in reporter.events if event.name == "progress_end"]
        self.assertEqual(len(ends), 4)
        self.assertEqual(progress[1]["percent"], 100)
        self.assertEqual(progress[2]["label"], "Reading data track 02")
        self.assertEqual(progress[4]["scsi"], 0)
        self.assertEqual(progress[4]["c2"], 2)
        self.assertEqual(progress[6]["scsi"], 1)
        self.assertEqual(progress[6]["c2"], 0)

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
            line = "[ 25%] errors: { SCSIs: 1, C2s: 2, Q: 3 }\n"
            observer(line)
            observer(line)
            return CommandResult(tuple(command), 0, "text")

        runner.run_streaming.side_effect = percentage
        RedumperProcessExecutor(runner, reporter).run(["redumper", "dump"])
        progress = next(event for event in reporter.events if event.name == "progress")
        self.assertEqual(progress.values["label"], "Reading")
        self.assertEqual(progress.values["c2"], 2)
