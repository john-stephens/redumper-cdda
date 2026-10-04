"""Contract tests for the production composition root."""

import unittest
from pathlib import Path
from unittest import mock

from redumper_cdda import bootstrap
from redumper_cdda.adapters.accuraterip import AccurateRipVerifier, NullVerifier
from redumper_cdda.adapters.console import (
    ConciseReporter,
    MultiplexReporter,
    QuietReporter,
    VerboseReporter,
)
from redumper_cdda.application.workflow import ExtractionApplication


class BootstrapTests(unittest.TestCase):
    def test_builds_production_object_graph(self):
        application = bootstrap.create_application(QuietReporter())
        self.assertIsInstance(application, ExtractionApplication)

    def test_existing_dump_configures_matching_mmc_toc_files(self):
        reporter = QuietReporter()
        with (
            mock.patch.object(bootstrap, "MmcTocReader") as mmc_reader,
            mock.patch.object(bootstrap, "CdparanoiaTocReader") as audio_reader,
        ):
            application = bootstrap.create_application(
                reporter,
                existing_dump=Path("/archive/disc"),
                cdparanoia_toc_file=Path("/archive/cd.txt"),
            )

        self.assertIsInstance(application, ExtractionApplication)
        mmc_reader.assert_called_once_with(
            mock.ANY,
            reporter,
            Path("/archive/disc.toc"),
            Path("/archive/disc.fulltoc"),
        )
        audio_reader.assert_called_once_with(
            mock.ANY, reporter, Path("/archive/cd.txt")
        )

    def test_conversion_diagnostics_go_only_to_verbose_reporters(self):
        terminal = []
        log = []
        reporter = MultiplexReporter(
            ConciseReporter(terminal.append),
            VerboseReporter(log.append),
        )

        bootstrap._conversion_output(reporter)("WAV conversion")

        self.assertEqual(terminal, [])
        self.assertEqual(log, ["WAV conversion"])

    def test_verifier_factory_disabled_missing_and_available(self):
        self.assertIsInstance(bootstrap._verifier(False), NullVerifier)
        with mock.patch.object(
            bootstrap, "load_accuraterip_library", side_effect=RuntimeError("missing")
        ):
            self.assertIsInstance(bootstrap._verifier(True), NullVerifier)
        with mock.patch.object(
            bootstrap, "load_accuraterip_library", return_value={"library": True}
        ):
            self.assertIsInstance(bootstrap._verifier(True), AccurateRipVerifier)


if __name__ == "__main__":
    unittest.main()
