import unittest
from types import SimpleNamespace
from unittest import mock

from redumper_cdda.application.acquisition import AcquisitionService
from redumper_cdda.domain.errors import DumpError, IntegrityStatusError, RefineError
from redumper_cdda.domain.integrity import MediaErrors
from redumper_cdda.ports.process import CommandResult
from redumper_cdda.domain.extraction import SectorRange
from tests.contracts.fakes import RecordingReporter


class AcquisitionServiceTests(unittest.TestCase):
    def service(self, dump, errors, refine=None):
        redumper = mock.Mock()
        redumper.dump.return_value = dump
        redumper.refine.return_value = refine or CommandResult((), 0, "refine")
        parser = mock.Mock()
        parser.media_errors.side_effect = errors
        reporter = RecordingReporter()
        return AcquisitionService(redumper, parser, reporter), redumper, reporter

    def test_refines_only_until_scsi_and_c2_are_clear(self):
        plan = SimpleNamespace(
            physical_range=SectorRange(100, 201)
        )
        request = SimpleNamespace(
            refine_passes=3,
            abort_on_skip=False, single_file=False
        )
        redumper = mock.Mock()
        redumper.dump.return_value = CommandResult((), 0, "dirty")
        redumper.refine.return_value = CommandResult((), 0, "clean")
        parser = mock.Mock()
        parser.media_errors.side_effect = (
            MediaErrors(1, 0, 9),
            MediaErrors(0, 0, 9),
        )

        reporter = RecordingReporter()
        result = AcquisitionService(redumper, parser, reporter).acquire(plan, request)

        self.assertEqual(result.media_errors, MediaErrors(0, 0, 9))
        self.assertEqual(result.refine_passes_used, 1)
        redumper.dump.assert_called_once_with(plan)
        redumper.refine.assert_called_once_with(plan)
        self.assertIn("refinement_started", [event.name for event in reporter.events])

    def test_dump_and_refine_failures(self):
        plan = SimpleNamespace(physical_range=SectorRange(0, 2))
        request = SimpleNamespace(
            refine_passes=1,
            abort_on_skip=False, single_file=False
        )
        service, _redumper, _reporter = self.service(
            CommandResult((), 2, ""), []
        )
        with self.assertRaises(DumpError):
            service.acquire(plan, request)

        service, _redumper, _reporter = self.service(
            CommandResult((), 0, "dirty"), [MediaErrors(1, 0, 0)],
            CommandResult((), 2, "failed"),
        )
        with self.assertRaises(RefineError):
            service.acquire(plan, request)

    def test_unparseable_status_before_and_after_refine(self):
        plan = SimpleNamespace(physical_range=SectorRange(0, 2))
        request = SimpleNamespace(
            refine_passes=1,
            abort_on_skip=False, single_file=False
        )
        failure = IntegrityStatusError("missing")
        service, _redumper, _reporter = self.service(
            CommandResult((), 0, "bad"), [failure]
        )
        with self.assertRaisesRegex(IntegrityStatusError, "from dump"):
            service.acquire(plan, request)

        service, _redumper, _reporter = self.service(
            CommandResult((), 0, "dirty"), [MediaErrors(1, 0, 0), failure]
        )
        with self.assertRaisesRegex(IntegrityStatusError, "after refine"):
            service.acquire(plan, request)

    def test_unresolved_error_policies_and_pluralization(self):
        plan = SimpleNamespace(physical_range=SectorRange(0, 2))
        dirty = MediaErrors(1, 2, 0)
        for passes, suffix in ((1, "pass."), (2, "passes.")):
            request = SimpleNamespace(
                refine_passes=passes,
                abort_on_skip=True,
                single_file=True,
            )
            errors = [dirty] * (passes + 1)
            service, _redumper, _reporter = self.service(
                CommandResult((), 0, "dirty"), errors
            )
            with self.subTest(passes=passes), self.assertRaisesRegex(
                IntegrityStatusError, suffix
            ):
                service.acquire(plan, request)

        request = SimpleNamespace(
            refine_passes=1,
            abort_on_skip=False, single_file=False
        )
        service, _redumper, reporter = self.service(
            CommandResult((), 0, "dirty"), [dirty, dirty]
        )
        result = service.acquire(plan, request)
        self.assertTrue(result.media_errors.has_data_errors)
        self.assertEqual(reporter.events[-1].name, "warning")

    def test_refine_forever_ignores_pass_limit_and_stops_when_clean(self):
        plan = SimpleNamespace(physical_range=SectorRange(0, 2))
        request = SimpleNamespace(
            refine_passes=None,
            abort_on_skip=False,
            single_file=False,
        )
        dirty = MediaErrors(0, 1, 2)
        service, redumper, reporter = self.service(
            CommandResult((), 0, "dirty"),
            [dirty, dirty, MediaErrors(0, 0, 1)],
        )

        result = service.acquire(plan, request)

        self.assertEqual(result.refine_passes_used, 2)
        self.assertEqual(redumper.refine.call_count, 2)
        refinements = [
            event for event in reporter.events
            if event.name == "refinement_started"
        ]
        self.assertEqual(refinements[0].values["maximum"], None)


if __name__ == "__main__":
    unittest.main()
