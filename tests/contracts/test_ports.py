import unittest

from redumper_cdda.domain.events import LifecycleEvent
from redumper_cdda.ports import acquisition, layout, output, process, reporting, verification
from tests.contracts.fakes import RecordingReporter


class PortContractTests(unittest.TestCase):
    def test_contracts_are_importable_and_reporter_fake_obeys_contract(self):
        contracts = (
            acquisition.RedumperPort,
            acquisition.IntegrityParser,
            acquisition.StateInspector,
            layout.LayoutProvider,
            output.OutputWriter,
            output.OutputService,
            process.ProcessRunner,
            reporting.Reporter,
            verification.Verifier,
        )
        self.assertTrue(all(contract is not None for contract in contracts))
        reporter = RecordingReporter()
        event = LifecycleEvent("test")
        reporter.publish(event)
        self.assertEqual(reporter.events, [event])


if __name__ == "__main__":
    unittest.main()
