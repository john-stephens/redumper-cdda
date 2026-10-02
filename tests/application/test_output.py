import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from redumper_cdda.application.output import OutputPlanner, OutputTransaction
from redumper_cdda.domain.disc import Track, TrackKind
from redumper_cdda.domain.errors import OutputError, PlanningError
from redumper_cdda.domain.extraction import ResolvedSelection
from redumper_cdda.domain.outputs import OutputKind, OutputPlan, ResolvedOutput


class ContentWriter:
    def __init__(self, content, fail=False):
        self.content = content
        self.fail = fail

    def write(self, _resolved, path, verbose=False):
        if self.fail:
            raise RuntimeError("write failed")
        path.write_bytes(self.content)


class OutputPlannerTests(unittest.TestCase):
    @staticmethod
    def request(**changes):
        values = {"single_file": False, "output": None, "prefix": "track"}
        values.update(changes)
        return SimpleNamespace(**values)

    def test_plans_separate_audio_and_data_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Track(1, TrackKind.AUDIO, 0, 0, 10)
            data = Track(2, TrackKind.DATA, 4, 10, 30)
            plans = OutputPlanner(directory).create(
                ResolvedSelection((audio, data)), self.request(prefix="album")
            )
            self.assertEqual(
                [plan.output_path.name for plan in plans],
                ["album01.wav", "album02.iso"],
            )
            self.assertEqual([plan.kind for plan in plans], [OutputKind.AUDIO, OutputKind.DATA])

    def test_plans_single_audio_combined_audio_and_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio1 = Track(1, TrackKind.AUDIO, 0, 0, 10)
            audio2 = Track(2, TrackKind.AUDIO, 0, 10, 20)
            planner = OutputPlanner(root)
            single = planner.create(
                ResolvedSelection((audio1,)), self.request(single_file=True)
            )[0]
            self.assertEqual(single.output_path.name, "track01.wav")
            combined = planner.create(
                ResolvedSelection((audio1, audio2)),
                self.request(single_file=True, output=root / "chosen.wav"),
            )[0]
            self.assertEqual(combined.output_path, (root / "chosen.wav").resolve())
            self.assertEqual(combined.expected_sectors, 20)

            data = Track(1, TrackKind.DATA, 4, 0, 10)
            data_plan = planner.create(
                ResolvedSelection((data,)), self.request(single_file=True)
            )[0]
            self.assertEqual(data_plan.output_path.name, "track01.iso")
            with self.assertRaisesRegex(PlanningError, "separate files"):
                planner.create(
                    ResolvedSelection((audio1, data)),
                    self.request(single_file=True),
                )


class OutputTransactionTests(unittest.TestCase):
    @staticmethod
    def resolved(path, kind):
        return ResolvedOutput(OutputPlan(None, kind, (), 1, path))

    def test_commits_complete_set_and_removes_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = self.resolved(root / "one.wav", OutputKind.AUDIO)
            data = self.resolved(root / "two.iso", OutputKind.DATA)
            audio.plan.output_path.write_bytes(b"old")
            (root / ".one.wav.part").write_bytes(b"stale")
            (root / ".two.iso.backup").write_bytes(b"stale")
            transaction = OutputTransaction(
                {
                    OutputKind.AUDIO: ContentWriter(b"audio"),
                    OutputKind.DATA: ContentWriter(b"data"),
                }
            )
            completed = transaction.create((audio, data))
            self.assertEqual(audio.plan.output_path.read_bytes(), b"audio")
            self.assertEqual(data.plan.output_path.read_bytes(), b"data")
            self.assertEqual([item.path for item in completed], [audio.plan.output_path, data.plan.output_path])
            self.assertFalse((root / ".one.wav.backup").exists())

    def test_writer_failure_preserves_existing_output_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.resolved(root / "one.wav", OutputKind.AUDIO)
            output.plan.output_path.write_bytes(b"old")
            transaction = OutputTransaction(
                {OutputKind.AUDIO: ContentWriter(b"new", fail=True)}
            )
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                transaction.create((output,))
            self.assertEqual(output.plan.output_path.read_bytes(), b"old")
            self.assertFalse((root / ".one.wav.part").exists())

    def test_commit_failure_rolls_back_every_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.resolved(root / "one.wav", OutputKind.AUDIO)
            second = self.resolved(root / "two.wav", OutputKind.AUDIO)
            first.plan.output_path.write_bytes(b"old one")
            second.plan.output_path.write_bytes(b"old two")
            transaction = OutputTransaction(
                {OutputKind.AUDIO: ContentWriter(b"new")}
            )
            original_replace = Path.replace

            def replace(path, target):
                if path.name == ".two.wav.part":
                    raise OSError("commit failed")
                return original_replace(path, target)

            with (
                mock.patch.object(Path, "replace", autospec=True, side_effect=replace),
                self.assertRaisesRegex(OSError, "commit failed"),
            ):
                transaction.create((first, second))
            self.assertEqual(first.plan.output_path.read_bytes(), b"old one")
            self.assertEqual(second.plan.output_path.read_bytes(), b"old two")

    def test_rejects_duplicate_paths(self):
        output = self.resolved(Path("same.wav"), OutputKind.AUDIO)
        with self.assertRaisesRegex(OutputError, "unique"):
            OutputTransaction({}).create((output, output))

    def test_backup_cleanup_failure_does_not_rollback_committed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = self.resolved(root / "one.wav", OutputKind.AUDIO)
            output.plan.output_path.write_bytes(b"old")
            transaction = OutputTransaction(
                {OutputKind.AUDIO: ContentWriter(b"new")}
            )
            original_unlink = Path.unlink

            def unlink(path, *args, **kwargs):
                if path.name == ".one.wav.backup":
                    raise OSError("cannot remove backup")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(
                Path, "unlink", autospec=True, side_effect=unlink
            ):
                transaction.create((output,))
            self.assertEqual(output.plan.output_path.read_bytes(), b"new")
            self.assertTrue((root / ".one.wav.backup").exists())


if __name__ == "__main__":
    unittest.main()
