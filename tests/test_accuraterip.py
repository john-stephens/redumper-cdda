"""Tests for AccurateRip identification and verification."""

import builtins
import warnings
import importlib
import io
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

SRC_PATH = Path(__file__).parent.parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from redumper_cdda import accuraterip


class AccurateRipCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.reload(accuraterip)

    @staticmethod
    def audio_track(number=1):
        return {
            "number": number,
            "kind": "audio",
            "begin": (number - 1) * 10,
            "end": number * 10,
            "length": 10,
            "length_msf": "00:00.10",
        }

    def library(self, database=None, checksum_error=None):
        fetch = mock.Mock()
        fetch.from_id.return_value.fetch.return_value = database
        checksums = mock.Mock(arv1=1, arv2=2)
        get_checksums = mock.Mock(return_value=checksums)
        if checksum_error is not None:
            get_checksums.side_effect = checksum_error
        return {
            "get_checksums": get_checksums,
            "fetcher": fetch,
            "accuraterip_ids": mock.Mock(return_value=(11, 22)),
            "freedb_id": mock.Mock(return_value=33),
        }

    def test_load_accuraterip_library_success_and_failure(self):
        fake_checksums = mock.Mock()
        fake_fetcher = mock.Mock()
        fake_ids = mock.Mock()
        fake_freedb = mock.Mock()
        modules = {
            "arver.audio.checksums": mock.Mock(get_checksums=fake_checksums),
            "arver.disc.database": mock.Mock(AccurateRipFetcher=fake_fetcher),
            "arver.disc.fingerprint": mock.Mock(
                accuraterip_ids=fake_ids, freedb_id=fake_freedb
            ),
        }

        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name in modules:
                return modules[name]
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            loaded = self.module.load_accuraterip_library()
        self.assertIs(loaded["get_checksums"], fake_checksums)

        def failed_import(name, *args, **kwargs):
            if name.startswith("arver"):
                raise ImportError("missing")
            return original_import(name, *args, **kwargs)

        with (
            mock.patch("builtins.__import__", side_effect=failed_import),
            self.assertRaisesRegex(RuntimeError, "requires the ARver"),
        ):
            self.module.load_accuraterip_library()

    def test_accuraterip_layout_variants_and_default_loader(self):
        audio = self.audio_track()
        data = dict(audio, number=2, kind="data")
        with self.assertRaisesRegex(RuntimeError, "at least one audio"):
            self.module.accuraterip_layout_type([])
        self.assertEqual(self.module.accuraterip_layout_type([data, audio]), "mixed-mode")
        self.assertEqual(self.module.accuraterip_layout_type([audio, data]), "enhanced")
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            self.module.accuraterip_layout_type([audio, data, audio])

        library = self.library()
        with mock.patch.object(
            self.module, "load_accuraterip_library", return_value=library
        ) as load:
            disc_id = self.module.build_accuraterip_disc_id([audio])
        self.assertEqual(disc_id, "001-11-22-33")
        load.assert_called_once_with()

    def test_accuraterip_no_match_and_error_paths(self):
        self.assertEqual(
            self.module.match_accuraterip_checksums(1, 2, {3: {}})["status"],
            "no-match",
        )
        track = self.audio_track()
        verification = [{"track": track, "segments": []}]

        with mock.patch.object(
            self.module, "load_accuraterip_library", return_value=self.library()
        ):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                self.module.verify_with_accuraterip([track], verification, Path("."))

        fetch_error = self.library()
        fetch_error["fetcher"].from_id.side_effect = ValueError("network")
        with self.assertRaisesRegex(RuntimeError, "database lookup failed"):
            self.module.verify_with_accuraterip(
                [track], verification, Path("."), library=fetch_error
            )

        database = mock.Mock()
        database.make_dict.side_effect = ValueError("bad data")
        with self.assertRaisesRegex(RuntimeError, "unusable database"):
            self.module.verify_with_accuraterip(
                [track], verification, Path("."), library=self.library(database)
            )

        database = mock.Mock()
        database.make_dict.return_value = {}
        checksum_error = self.library(database, OSError("bad wav"))
        with (
            mock.patch.object(self.module, "segments_to_wav"),
            self.assertRaisesRegex(RuntimeError, "Could not checksum"),
        ):
            self.module.verify_with_accuraterip(
                [track], verification, Path("."), library=checksum_error
            )

        missing = self.library()
        missing["fetcher"].from_id.return_value.fetch.side_effect = (
            lambda: print("database detail")
        )
        with self.assertRaisesRegex(RuntimeError, "database detail"):
            self.module.verify_with_accuraterip(
                [track], verification, Path("."), library=missing
            )

    def test_accuraterip_report_all_statuses(self):
        report = {
            "disc_id": "disc",
            "results": [
                {
                    "track": 1, "status": "verified", "version": "ARv2",
                    "checksum": 2, "confidence": 4, "response": "ok",
                },
                {"track": 2, "status": "not-present", "arv1": 1, "arv2": 2},
                {"track": 3, "status": "no-match", "arv1": 3, "arv2": 4},
            ],
        }
        with redirect_stdout(io.StringIO()) as output:
            self.module.print_accuraterip_report(report, verbose=True)
        rendered = output.getvalue()
        self.assertIn("response ok", rendered)
        self.assertIn("not present", rendered)
        self.assertIn("no match", rendered)

        report["results"] = report["results"][:1]
        with redirect_stdout(io.StringIO()) as output:
            self.module.print_accuraterip_report(report, verbose=False)
        self.assertNotIn("response ok", output.getvalue())
