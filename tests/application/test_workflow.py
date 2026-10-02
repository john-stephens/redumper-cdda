import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from redumper_cdda.application.workflow import ExtractionApplication
from redumper_cdda.domain.errors import IntegrityStatusError, OutputError, VerificationError
from redumper_cdda.domain.integrity import MediaErrors
from redumper_cdda.domain.outputs import CompletedOutput, SplitResult, VerificationReport
from tests.contracts.fakes import RecordingReporter


class FakeWorkspaceFactory:
    def __init__(self):
        self.workspace = SimpleNamespace(
            path=Path("/work"), snapshot=lambda: {"before": True},
            changed_files=lambda before: [before],
        )

    @contextmanager
    def create(self):
        yield self.workspace


class ExtractionApplicationTests(unittest.TestCase):
    def setUp(self):
        self.layout = mock.Mock()
        self.disc = object()
        self.layout.read.return_value = self.disc
        self.planner = mock.Mock()
        self.plan = SimpleNamespace(outputs=())
        self.planner.create.return_value = self.plan
        self.acquirer = mock.Mock()
        self.acquisition = SimpleNamespace(media_errors=MediaErrors(0, 0, 0))
        self.acquirer.acquire.return_value = self.acquisition
        self.splitter = mock.Mock()
        self.split = SplitResult(())
        self.splitter.split.return_value = self.split
        self.output = mock.Mock()
        self.output.create.return_value = ()
        self.verifier = mock.Mock()
        self.reporter = RecordingReporter()
        self.workspace = FakeWorkspaceFactory()
        self.application = ExtractionApplication(
            self.layout, self.planner, self.acquirer, self.splitter,
            self.output, lambda _enabled: self.verifier, self.workspace,
            self.reporter,
        )

    def request(self, accuraterip=False):
        return SimpleNamespace(device="drive", accuraterip=accuraterip)

    def test_read_layout_and_successful_run(self):
        self.assertIs(self.application.read_layout("drive"), self.disc)
        result = self.application.run(self.request())
        self.assertIs(result.plan, self.plan)
        self.assertIsNone(result.verification)
        self.verifier.prepare.assert_called_once_with(self.plan)
        self.splitter.split.assert_called_once_with(
            self.plan, mock.ANY, self.acquisition, {"before": True},
            self.workspace.workspace.changed_files,
        )
        self.assertEqual(
            [event.name for event in self.reporter.events],
            ["plan", "output_started", "output_finished", "complete"],
        )

    def test_verification_runs_after_output(self):
        report = VerificationReport("id", ())
        self.verifier.verify.return_value = report
        result = self.application.run(self.request(accuraterip=True))
        self.assertIs(result.verification, report)
        names = [event.name for event in self.reporter.events]
        self.assertLess(names.index("output_finished"), names.index("verification_started"))
        self.assertIn("verification_report", names)

    def test_disabled_verifier_skips_verification_even_when_requested(self):
        self.verifier.enabled = False
        result = self.application.run(self.request(accuraterip=True))
        self.assertIsNone(result.verification)
        self.verifier.verify.assert_not_called()

    def test_resolved_outputs_publish_detail_events(self):
        resolved = object()
        self.splitter.split.return_value = SplitResult((resolved,))
        self.application.run(self.request())
        details = [
            event.values for event in self.reporter.events
            if event.name == "output_detail"
        ]
        self.assertEqual(details, [resolved])

    def test_verification_failure_retains_outputs(self):
        self.verifier.verify.side_effect = VerificationError("network")
        with self.assertRaisesRegex(VerificationError, "retained"):
            self.application.run(self.request(accuraterip=True))
        self.output.create.assert_called_once()

    def test_output_failures_are_reported_and_typed(self):
        self.output.create.side_effect = RuntimeError("disk")
        with self.assertRaisesRegex(OutputError, "disk"):
            self.application.run(self.request())
        self.assertEqual(self.reporter.events[-1].values, False)

        self.output.create.side_effect = OutputError("typed")
        with self.assertRaisesRegex(OutputError, "typed"):
            self.application.run(self.request())

        self.output.create.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.application.run(self.request())

    def test_omitted_outputs_raise_after_clean_outputs_commit(self):
        omitted = SimpleNamespace(
            plan=SimpleNamespace(track=SimpleNamespace(number=2)),
            media_errors=object(),
        )
        self.splitter.split.return_value = SplitResult((), (omitted,))
        with self.assertRaisesRegex(IntegrityStatusError, "Track 02"):
            self.application.run(self.request())
        self.output.create.assert_called_once()
        self.assertIn("omitted", [event.name for event in self.reporter.events])

        self.reporter.events.clear()
        omitted2 = SimpleNamespace(
            plan=SimpleNamespace(track=SimpleNamespace(number=3)),
            media_errors=object(),
        )
        self.splitter.split.return_value = SplitResult((), (omitted, omitted2))
        with self.assertRaisesRegex(IntegrityStatusError, "Tracks 02, 03"):
            self.application.run(self.request())


if __name__ == "__main__":
    unittest.main()
