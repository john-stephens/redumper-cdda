"""Contract tests for the production composition root."""

import unittest
from unittest import mock

from redumper_cdda import bootstrap
from redumper_cdda.adapters.accuraterip import AccurateRipVerifier, NullVerifier
from redumper_cdda.adapters.console import QuietReporter
from redumper_cdda.application.workflow import ExtractionApplication


class BootstrapTests(unittest.TestCase):
    def test_builds_production_object_graph(self):
        application = bootstrap.create_application(QuietReporter())
        self.assertIsInstance(application, ExtractionApplication)

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
