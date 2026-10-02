import tempfile
import unittest
from pathlib import Path
from unittest import mock

from redumper_cdda.adapters.workspace import TemporaryWorkspaceFactory, Workspace
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
            with mock.patch.object(Path, "stat", side_effect=OSError):
                self.assertEqual(workspace.snapshot(), {})
                self.assertEqual(workspace.changed_files({}), [])

    def test_reports_removal_when_body_fails(self):
        reporter = RecordingReporter()
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with TemporaryWorkspaceFactory(reporter).create():
                raise RuntimeError("boom")
        self.assertEqual(reporter.events[-1].name, "workspace_removed")

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
