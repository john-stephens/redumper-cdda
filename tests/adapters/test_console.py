import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from redumper_cdda.adapters.console import (
    ConciseReporter, MultiplexReporter, QuietReporter, VerboseReporter,
    reporter_for,
)
from redumper_cdda.domain.disc import DiscLayout, Track, TrackKind
from redumper_cdda.domain.events import LifecycleEvent
from redumper_cdda.domain.integrity import MediaErrors, TrackMediaErrors
from redumper_cdda.domain.outputs import OutputKind, VerificationReport


class ConsoleReporterTests(unittest.TestCase):
    def setUp(self):
        self.lines = []

        def output(*values, **options):
            self.lines.append((" ".join(str(value) for value in values), options))

        self.output = output
        self.track = Track(1, TrackKind.AUDIO, 0, 10, 20, "00:00.10")
        self.data = Track(2, TrackKind.DATA, 4, 20, 30, "00:00.10")
        self.output_plan = SimpleNamespace(
            output_path=Path("track01.wav"), kind=OutputKind.AUDIO, track=self.track
        )
        self.plan = SimpleNamespace(
            selection=SimpleNamespace(tracks=(self.track,)),
            track_label="01", logical_start_lba=10, logical_end_lba=20,
            dump_start_lba=10, dump_end_lba=21, expected_sectors=10,
            outputs=(self.output_plan,),
        )

    def test_quiet_and_factory(self):
        self.assertIsNone(QuietReporter().publish(LifecycleEvent("anything")))
        self.assertIsInstance(reporter_for(quiet=True), QuietReporter)
        self.assertIsInstance(reporter_for(verbose=True), VerboseReporter)
        self.assertIsInstance(reporter_for(), ConciseReporter)

        first = mock.Mock(verbose=False)
        second = mock.Mock(verbose=True)
        multiplex = MultiplexReporter(first, second)
        event = LifecycleEvent("anything")
        multiplex.publish(event)
        self.assertTrue(multiplex.verbose)
        first.publish.assert_called_once_with(event)
        second.publish.assert_called_once_with(event)

    def test_concise_all_events(self):
        reporter = ConciseReporter(self.output)
        request = SimpleNamespace(single_file=False)
        reporter.publish(LifecycleEvent("plan", {"plan": self.plan, "request": request}))
        request.single_file = True
        reporter.publish(LifecycleEvent("plan", {"plan": self.plan, "request": request}))
        reporter.publish(
            LifecycleEvent(
                "progress",
                {"label": "Reading", "percent": 50, "scsi": 1, "c2": 2, "q": 3},
            )
        )
        reporter.publish(LifecycleEvent("progress_end"))
        reporter.publish(LifecycleEvent("warning", "dirty"))
        reporter.publish(LifecycleEvent("split_started"))
        reporter.publish(LifecycleEvent("split_finished", True))
        reporter.publish(LifecycleEvent("split_finished", False))
        resolved = SimpleNamespace(plan=self.output_plan)
        reporter.publish(LifecycleEvent("output_started", {"outputs": (resolved, resolved)}))
        reporter.publish(LifecycleEvent("output_started", {"outputs": (resolved,)}))
        iso = SimpleNamespace(plan=SimpleNamespace(kind=OutputKind.DATA))
        reporter.publish(LifecycleEvent("output_started", {"outputs": (iso,)}))
        reporter.publish(LifecycleEvent("output_finished", True))
        reporter.publish(LifecycleEvent("output_finished", False))
        reporter.publish(LifecycleEvent("verification_started"))
        report = VerificationReport(
            "disc",
            (
                {"track": 1, "status": "verified", "version": "ARv2", "checksum": 2, "confidence": 3, "response": "ok"},
                {"track": 2, "status": "not-present", "arv1": 1, "arv2": 2},
                {"track": 3, "status": "no-match", "arv1": 3, "arv2": 4},
            ),
        )
        reporter.publish(LifecycleEvent("verification_report", report))
        reporter.publish(
            LifecycleEvent(
                "verification_report", VerificationReport("one", (report.results[0],))
            )
        )
        omitted = SimpleNamespace(
            plan=SimpleNamespace(track=self.track),
            media_errors=TrackMediaErrors(2, 3, 1, 1),
        )
        reporter.publish(LifecycleEvent("omitted", omitted))
        clean = SimpleNamespace(acquisition=SimpleNamespace(media_errors=MediaErrors(0, 0, 1)), omitted=())
        reporter.publish(LifecycleEvent("complete", clean))
        reporter.publish(
            LifecycleEvent(
                "complete",
                SimpleNamespace(
                    acquisition=SimpleNamespace(
                        media_errors=MediaErrors(0, 0, None)
                    ),
                    omitted=(),
                ),
            )
        )
        reporter.publish(LifecycleEvent("complete", SimpleNamespace(acquisition=clean.acquisition, omitted=(omitted,))))
        reporter.publish(LifecycleEvent("complete", SimpleNamespace(acquisition=clean.acquisition, omitted=(omitted, omitted))))
        reporter.publish(LifecycleEvent("disc_layout", DiscLayout((self.track, self.data), 30)))
        reporter.publish(
            LifecycleEvent(
                "existing_dump_staged",
                {"source": Path("disc"), "files": (Path("disc.state"),)},
            )
        )
        reporter.publish(LifecycleEvent("unknown"))
        rendered = "\n".join(line for line, _options in self.lines)
        self.assertIn("Ripping track 01", rendered)
        self.assertIn("Reading:  50% SCSI=1 C2=2 Q=3", rendered)
        self.assertIn("Skipping Track 01", rendered)
        self.assertIn("data", rendered)
        self.assertIn("Q=unavailable", rendered)

    def test_verbose_all_events(self):
        reporter = VerboseReporter(self.output)
        result = SimpleNamespace(
            plan=self.plan,
            acquisition=SimpleNamespace(
                media_errors=MediaErrors(1, 0, 2), refine_passes_used=1
            ),
        )
        report = SimpleNamespace(
            disc_id="id",
            results=(
                {"track": 1, "status": "verified", "version": "ARv2", "checksum": 2, "confidence": 3, "response": "ok"},
                {"track": 2, "status": "not-present", "arv1": 1, "arv2": 2},
                {"track": 3, "status": "no-match", "arv1": 3, "arv2": 4},
            ),
        )
        resolved = SimpleNamespace(
            plan=self.output_plan, pregap_skipped=2, cue_path=Path("disc.cue")
        )
        combined = SimpleNamespace(
            plan=SimpleNamespace(
                kind=OutputKind.AUDIO, track=None, output_path=Path("all.wav")
            ),
            pregap_skipped=None,
            cue_path=Path("all.cue"),
        )
        data = SimpleNamespace(
            plan=SimpleNamespace(
                kind=OutputKind.DATA, track=self.data, output_path=Path("data.iso")
            ),
            pregap_skipped=1,
            cue_path=Path("data.cue"),
        )
        events = (
            LifecycleEvent("workspace_created", Path("/tmp/work")),
            LifecycleEvent("workspace_removed", Path("/tmp/work")),
            LifecycleEvent("layout_read", "reading"),
            LifecycleEvent("command_started", ("redumper", "dump")),
            LifecycleEvent("tool_output", "line\n"),
            LifecycleEvent("plan", {"plan": self.plan}),
            LifecycleEvent("acquisition_started"),
            LifecycleEvent("refinement_started", {"pass_number": 1, "maximum": 2, "range": SimpleNamespace(start_lba=10, end_lba=21)}),
            LifecycleEvent("refinement_started", {"pass_number": 2, "maximum": None, "range": SimpleNamespace(start_lba=10, end_lba=21)}),
            LifecycleEvent("media_errors", {"errors": MediaErrors(1, 2, 3)}),
            LifecycleEvent("media_errors", {"errors": MediaErrors(0, 0, None)}),
            LifecycleEvent(
                "existing_dump_staged",
                {
                    "source": Path("disc"),
                    "files": (Path("disc.state"), Path("disc.scram")),
                },
            ),
            LifecycleEvent("split_started"),
            LifecycleEvent("omitted", SimpleNamespace(plan=SimpleNamespace(track=self.track), media_errors=TrackMediaErrors(1, 1, 1, 1))),
            LifecycleEvent("output_detail", resolved),
            LifecycleEvent("output_detail", combined),
            LifecycleEvent("output_detail", data),
            LifecycleEvent("verification_report", report),
            LifecycleEvent("complete", result),
            LifecycleEvent(
                "complete",
                SimpleNamespace(
                    plan=self.plan,
                    acquisition=SimpleNamespace(
                        media_errors=MediaErrors(0, 0, None),
                        refine_passes_used=0,
                    ),
                ),
            ),
            LifecycleEvent("disc_layout", DiscLayout((self.track,), 20)),
            LifecycleEvent("unknown"),
        )
        for event in events:
            reporter.publish(event)
        rendered = "\n".join(line for line, _options in self.lines)
        self.assertIn("Physical read range", rendered)
        self.assertIn("Refine pass 2 (until clean)", rendered)
        self.assertIn("WARNING", rendered)
        self.assertIn("Source prefix: disc", rendered)
        self.assertIn("Final Q errors:     unavailable", rendered)


if __name__ == "__main__":
    unittest.main()
