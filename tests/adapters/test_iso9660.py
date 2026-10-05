import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from redumper_cdda.adapters.iso9660 import IsoOutputWriter
from redumper_cdda.domain.errors import IsoOutputError
from redumper_cdda.domain.outputs import DataTrackSource, OutputKind, OutputPlan, ResolvedOutput


class IsoOutputWriterTests(unittest.TestCase):
    def setUp(self):
        source = DataTrackSource(
            Path("disc.cue"), Path("data.bin"), 1,
            "MODE1/2352", 2352, 2, 10,
        )
        plan = OutputPlan(
            SimpleNamespace(begin_lba=100),
            OutputKind.DATA,
            (),
            10,
            Path("track01.iso"),
        )
        self.resolved = ResolvedOutput(plan, data_source=source)

    def test_delegates_typed_data_source_and_translates_errors(self):
        converter = mock.Mock()
        writer = IsoOutputWriter(converter)
        writer.write(self.resolved, Path("part"), verbose=True)
        converted = converter.call_args.args[0]
        self.assertEqual(converted["start_sector"], 2)
        self.assertEqual(converted["track_type"], "MODE1/2352")
        self.assertEqual(converted["track_begin_lba"], 100)
        self.assertEqual(converted["track_sectors"], 10)

        converter.side_effect = RuntimeError("bad iso")
        with self.assertRaisesRegex(IsoOutputError, "bad iso"):
            writer.write(self.resolved, Path("part"))


if __name__ == "__main__":
    unittest.main()
