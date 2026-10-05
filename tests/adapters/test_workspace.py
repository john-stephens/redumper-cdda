import tempfile
import unittest
import stat
from pathlib import Path
from unittest import mock

from redumper_cdda.adapters.workspace import TemporaryWorkspaceFactory, Workspace
from redumper_cdda.domain.errors import DumpError
from tests.contracts.fakes import RecordingReporter


class WorkspaceTests(unittest.TestCase):
    def test_snapshot_changed_files_and_lifecycle(self):
        reporter = RecordingReporter()
        with TemporaryWorkspaceFactory(reporter).create() as workspace:
            path = workspace.path
            before = workspace.snapshot()
            file_path = path / "file"
            file_path.write_text("one")
            self.assertEqual(workspace.changed_files(before), [file_path])
            stable = workspace.snapshot()
            self.assertEqual(workspace.changed_files(stable), [])
        self.assertFalse(path.exists())
        self.assertEqual([event.name for event in reporter.events], ["workspace_created", "workspace_removed"])

    def test_ignores_nonfiles_and_stat_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "folder").mkdir()
            file_path = workspace.path / "file"
            file_path.write_text("x")
            self.assertEqual(set(workspace.snapshot()), {file_path.resolve()})
            self.assertEqual(workspace.changed_files({}), [file_path])
            with mock.patch.object(Path, "stat", side_effect=OSError):
                self.assertEqual(workspace.snapshot(), {})
                self.assertEqual(workspace.changed_files({}), [])

    def test_reports_removal_when_body_fails(self):
        reporter = RecordingReporter()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with TemporaryWorkspaceFactory(reporter).create():
                raise RuntimeError("boom")
        self.assertEqual(reporter.events[-1].name, "workspace_removed")

    def test_stages_only_primary_dump_files_under_the_planned_name(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as work_dir:
            source = Path(source_dir) / "my.disc"
            for suffix in (".scram", ".state", ".subcode", ".toc", ".fulltoc", ".1.cache"):
                source.with_name(source.name + suffix).write_text(suffix)
            source.with_name(source.name + ".cue").write_text("stale")
            (Path(source_dir) / "my.disc-other.state").write_text("other")
            source.with_name(source.name + ".toc").chmod(stat.S_IRUSR)

            copied = Workspace(Path(work_dir)).stage_existing_dump(
                source, "track01"
            )

            self.assertEqual(
                {path.name for path in copied},
                {
                    "track01.scram", "track01.state", "track01.subcode",
                    "track01.toc", "track01.fulltoc", "track01.1.cache",
                },
            )
            self.assertFalse((Path(work_dir) / "track01.cue").exists())
            self.assertEqual(source.with_name(source.name + ".state").read_text(), ".state")
            self.assertFalse(
                source.with_name(source.name + ".toc").stat().st_mode
                & stat.S_IWUSR
            )
            self.assertTrue(
                (Path(work_dir) / "track01.toc").stat().st_mode & stat.S_IWUSR
            )

    def test_existing_dump_validation_and_copy_failures_are_typed(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as work_dir:
            workspace = Workspace(Path(work_dir))
            source = Path(source_dir) / "disc"
            source.with_suffix(".state").write_text("state")
            with self.assertRaisesRegex(DumpError, "incomplete"):
                workspace.stage_existing_dump(source, "track01")

            for suffix in (".scram", ".subcode", ".toc"):
                source.with_suffix(suffix).write_text(suffix)
            with mock.patch("redumper_cdda.adapters.workspace.shutil.copy2", side_effect=OSError("denied")):
                with self.assertRaisesRegex(DumpError, "Could not copy"):
                    workspace.stage_existing_dump(source, "track01")

        missing = Workspace(Path("/work"))
        with self.assertRaisesRegex(DumpError, "Could not read"):
            missing.stage_existing_dump(Path("/missing/directory/disc"), "track01")
        with self.assertRaisesRegex(DumpError, "filename prefix"):
            missing.stage_existing_dump(Path("/"), "track01")

    def test_factory_failure_does_not_mask_original_error(self):
        reporter = RecordingReporter()

        def fail(**_options):
            raise RuntimeError("factory")

        with self.assertRaisesRegex(RuntimeError, "factory"):
            with TemporaryWorkspaceFactory(reporter, fail).create():
                pass
        self.assertEqual(reporter.events, [])

if __name__ == "__main__":
    unittest.main()
