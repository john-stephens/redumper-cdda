import argparse
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import capture_physical_test_data as capture
from redumper_cdda.domain.disc import DiscLayout, Track, TrackKind


def track(number, kind, begin, end):
    return Track(number, kind, 0 if kind is TrackKind.AUDIO else 4, begin, end)


def disc(*tracks):
    return DiscLayout(tracks, tracks[-1].end_lba)


class CapturePhysicalTestDataTests(unittest.TestCase):
    def test_profile_scenarios_match_physical_plan(self):
        audio = disc(
            track(1, TrackKind.AUDIO, 0, 100),
            track(2, TrackKind.AUDIO, 100, 200),
            track(3, TrackKind.AUDIO, 200, 300),
        )
        pregap = disc(
            track(1, TrackKind.AUDIO, 10, 100),
            track(2, TrackKind.AUDIO, 100, 200),
        )
        data_first = disc(
            track(1, TrackKind.DATA, 0, 100),
            track(2, TrackKind.AUDIO, 100, 200),
            track(3, TrackKind.AUDIO, 200, 300),
        )
        data_last = disc(
            track(1, TrackKind.AUDIO, 0, 100),
            track(2, TrackKind.AUDIO, 100, 180),
            track(3, TrackKind.DATA, 200, 300),
        )
        data_only = disc(
            track(1, TrackKind.DATA, 0, 100),
            track(2, TrackKind.DATA, 100, 200),
        )
        self.assertEqual(len(capture.scenarios_for("regular-audio", audio)), 6)
        self.assertEqual(len(capture.scenarios_for("track0-pregap", pregap)), 5)
        self.assertEqual(len(capture.scenarios_for("data-first", data_first)), 4)
        data_last_scenarios = capture.scenarios_for("data-last", data_last)
        self.assertEqual(len(data_last_scenarios), 6)
        final_data = data_last_scenarios[-1]
        self.assertEqual(final_data.name, "d07-final-data-track")
        self.assertEqual(final_data.selection, capture.TrackSelection(3, 3))
        self.assertTrue(final_data.include_data)
        data_scenarios = capture.scenarios_for("data-only", data_only)
        self.assertEqual(len(data_scenarios), 2)
        self.assertTrue(all(item.include_data for item in data_scenarios))
        regular_scenarios = capture.scenarios_for("regular-audio", audio)
        fabricated = {
            item.name: item.fabricated_c2_track
            for item in regular_scenarios
            if item.fabricated_c2_track is not None
        }
        self.assertEqual(
            fabricated,
            {"a02-track-02": 2, "a03-a04-all-audio": 2},
        )
        regression = next(
            item for item in data_last_scenarios if item.name == "d05-all-tracks"
        )
        self.assertTrue(regression.include_data)
        self.assertEqual(regression.strict_accuraterip_track, 1)
        self.assertEqual(regression.required_write_offset_sign, -1)
        self.assertEqual(regression.omitted_alignment_track, 2)
        self.assertEqual(regression.fabricated_c2_track, 2)
        for profile, scenarios, expected in (
            ("regular-audio", capture.scenarios_for("regular-audio", audio), "a03-a04-all-audio"),
            ("data-last", data_last_scenarios, "d05-all-tracks"),
        ):
            with self.subTest(profile=profile):
                ordered = capture.capture_order(profile, scenarios)
                self.assertEqual(ordered[0].name, expected)
                self.assertCountEqual(ordered, scenarios)
        self.assertIs(
            capture.capture_order("data-only", data_scenarios), data_scenarios
        )

    def test_profile_validation_fails_closed(self):
        audio = disc(track(1, TrackKind.AUDIO, 0, 100))
        data = disc(track(1, TrackKind.DATA, 0, 100))
        cases = (
            ("regular-audio", audio),
            ("track0-pregap", audio),
            ("data-first", audio),
            ("data-last", data),
            ("data-only", audio),
            ("unknown", data),
        )
        for profile, layout in cases:
            with self.subTest(profile=profile):
                with self.assertRaises(capture.CaptureError):
                    capture.scenarios_for(profile, layout)

        offset_disc = disc(
            track(1, TrackKind.AUDIO, 0, 100),
            track(2, TrackKind.AUDIO, 100, 200),
            track(3, TrackKind.DATA, 220, 300),
        )
    def test_requests_and_layout_records_are_auditable(self):
        scenario = capture.Scenario(
            "mixed", ("case",), capture.TrackSelection(1, 2), True
        )
        request = capture.request_for("/dev/sg4", scenario, 25)
        self.assertEqual(request.device, "/dev/sg4")
        self.assertTrue(request.include_data)
        self.assertEqual(request.retries, 25)

        layout = disc(
            track(1, TrackKind.AUDIO, 0, 100),
            track(2, TrackKind.DATA, 100, 250),
        )
        record = capture.layout_record(layout)
        self.assertEqual(record["lead_out_lba"], 250)
        self.assertEqual(record["tracks"][1]["kind"], "data")
        self.assertEqual(record["tracks"][1]["length_sectors"], 150)

    def test_integer_range_and_inquiry_validation(self):
        self.assertEqual(capture.nonnegative_int("0"), 0)
        for value in ("-1", "bad"):
            with self.assertRaises(argparse.ArgumentTypeError):
                capture.nonnegative_int(value)
        inquiry = bytearray(36)
        inquiry[8:16] = b"VENDOR  "
        inquiry[16:32] = b"MODEL           "
        inquiry[32:36] = b"1.00"
        self.assertEqual(
            capture.parse_inquiry(bytes(inquiry)),
            {"vendor": "VENDOR", "model": "MODEL", "firmware_revision": "1.00"},
        )
        with self.assertRaises(capture.CaptureError):
            capture.parse_inquiry(b"short")
        with self.assertRaises(capture.CaptureError):
            capture.parse_inquiry(bytes(36))

    def test_captured_media_error_status_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "dump.log"
            log.write_text(
                "media errors:\n  SCSI: 0\n  C2: 0\n  Q: 3\n",
                encoding="utf-8",
            )
            self.assertEqual(capture.validate_clean_capture(log)["Q"], 3)

            log.write_text(
                "media errors:\n  SCSI: 1\n  C2: 2\n  Q: 0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(capture.CaptureError, "clean capture"):
                capture.validate_clean_capture(log)

            log.write_text("no status", encoding="utf-8")
            with self.assertRaisesRegex(capture.CaptureError, "could not determine"):
                capture.validate_clean_capture(log)

    def test_dump_validation_and_hash_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "disc"
            for suffix in (*capture.REQUIRED_DUMP_SUFFIXES, ".scram"):
                Path(f"{prefix}{suffix}").write_bytes(suffix.encode())
            capture.validate_dump(prefix)
            (root / "other").write_text("payload", encoding="utf-8")
            capture.write_hashes(root)
            sums = (root / "SHA256SUMS").read_text(encoding="utf-8")
            self.assertIn("disc.scram", sums)
            self.assertIn("other", sums)
            self.assertNotIn("SHA256SUMS", sums)
            Path(f"{prefix}.state").unlink()
            with self.assertRaisesRegex(capture.CaptureError, "missing .state"):
                capture.validate_dump(prefix)

    def test_automatic_write_offset_probe_and_profile_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "disc"
            for suffix in (*capture.REQUIRED_DUMP_SUFFIXES, ".scram"):
                Path(f"{prefix}{suffix}").write_bytes(suffix.encode())
            log = root / "probe.log"

            clean = SimpleNamespace(
                returncode=0, stdout=b"disc write offset: 0\n"
            )
            offsets, command = capture.probe_write_offsets(
                prefix, False, log, run=mock.Mock(return_value=clean)
            )
            self.assertEqual(offsets, ((0, 0),))
            self.assertNotIn("--force-offset=0", command)
            self.assertNotIn("--filesystem-trim", command)
            capture.validate_profile_write_offsets("regular-audio", offsets)

            shifted = SimpleNamespace(
                returncode=0, stdout=b"disc write offset: -12\n"
            )
            offsets, command = capture.probe_write_offsets(
                prefix, True, log, run=mock.Mock(return_value=shifted)
            )
            self.assertEqual(offsets, ((0, -12),))
            self.assertIn("--filesystem-trim", command)
            capture.validate_profile_write_offsets("data-last", offsets)

            with self.assertRaisesRegex(capture.CaptureError, "requires.*zero"):
                capture.validate_profile_write_offsets("regular-audio", offsets)
            with self.assertRaisesRegex(capture.CaptureError, "negative nonzero"):
                capture.validate_profile_write_offsets(
                    "data-last", ((0, 0),)
                )

            failed = SimpleNamespace(returncode=2, stdout=b"failed")
            with self.assertRaisesRegex(capture.CaptureError, "status 2"):
                capture.probe_write_offsets(
                    prefix, False, log, run=mock.Mock(return_value=failed)
                )
            malformed = SimpleNamespace(returncode=0, stdout=b"missing")
            with self.assertRaisesRegex(capture.CaptureError, "could not determine"):
                capture.probe_write_offsets(
                    prefix, False, log, run=mock.Mock(return_value=malformed)
                )

    def test_command_capture_and_streaming(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "case" / "output.log"
            output.parent.mkdir()
            records = []
            completed = SimpleNamespace(returncode=0, stdout=b"answer", stderr=b"")
            with mock.patch.object(capture.subprocess, "run", return_value=completed):
                self.assertEqual(
                    capture.capture_command(["tool"], output, records), b"answer"
                )
            self.assertEqual(output.read_bytes(), b"answer")
            self.assertEqual(records[0]["exit_status"], 0)

            class Process:
                def __init__(self):
                    self.stdout = io.BytesIO(b"streamed")
                    self.returncode = 0

                def wait(self, timeout=None):
                    return self.returncode

                def poll(self):
                    return self.returncode

            terminal = io.BytesIO()
            capture.stream_command(
                ["tool"], output, records, popen=lambda *args, **kwargs: Process(),
                terminal=terminal,
            )
            self.assertEqual(terminal.getvalue(), b"streamed")
            self.assertEqual(output.read_bytes(), b"streamed")

            failed = SimpleNamespace(returncode=2, stdout=b"bad", stderr=b"")
            with mock.patch.object(capture.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(capture.CaptureError, "status 2"):
                    capture.capture_command(["tool"], output, records)


if __name__ == "__main__":
    unittest.main()
