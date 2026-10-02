"""Tests for the command-line interface and extraction orchestration."""

import builtins
import importlib
import io
import os
import runpy
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from argparse import Namespace
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "redumper_cdda"
    / "cli.py"
)
SRC_PATH = SCRIPT_PATH.parents[1]

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from redumper_cdda import (
    accuraterip,
    cue,
    integrity,
    iso9660,
    layout,
    outputs,
    workflow,
)


def load_script():
    import redumper_cdda.cli

    return importlib.reload(redumper_cdda.cli)


class TemporaryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script()
        self.outputs = importlib.reload(outputs)
        self.workflow = importlib.reload(workflow)

    def run_main(self, extract_track):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "2",
        ]

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

    def test_workspace_removed_after_success(self):
        workspaces = []

        def extract_track(_args, workdir):
            workspaces.append(workdir)
            (workdir / "intermediate").write_bytes(
                b"data"
            )

        self.run_main(
            extract_track,
        )

        self.assertEqual(
            len(workspaces),
            1,
        )
        self.assertFalse(
            workspaces[0].exists()
        )

    def test_workspace_removed_after_interrupt(self):
        workspaces = []

        def extract_track(_args, workdir):
            workspaces.append(workdir)
            (workdir / "intermediate").write_bytes(
                b"data"
            )
            raise KeyboardInterrupt

        with self.assertRaises(
            KeyboardInterrupt
        ):
            self.run_main(
                extract_track,
            )

        self.assertFalse(
            workspaces[0].exists()
        )

    def test_workspace_removed_after_failure(self):
        workspaces = []

        def extract_track(_args, workdir):
            workspaces.append(workdir)
            (workdir / "intermediate").write_bytes(
                b"data"
            )
            raise SystemExit("failed")

        with self.assertRaises(SystemExit):
            self.run_main(
                extract_track,
            )

        self.assertFalse(
            workspaces[0].exists()
        )

    def test_termination_signal_becomes_exception(self):
        with self.assertRaises(
            self.module.TerminationRequested
        ) as caught:
            self.module.handle_termination_signal(
                signal.SIGTERM,
                None,
            )

        self.assertEqual(
            caught.exception.signum,
            signal.SIGTERM,
        )

    def test_show_layout_exits_without_dumping(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "--show-layout",
        ]
        tracks = [
            {
                "number": 1,
                "length": 75,
                "begin": 0,
                "end": 75,
            }
        ]

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "read_disc_layout",
                return_value=tracks,
            ),
            mock.patch.object(
                self.module,
                "print_disc_layout",
            ) as print_layout,
            mock.patch.object(
                self.module,
                "extract_track",
            ) as extract_track,
            mock.patch.object(
                self.module.tempfile,
                "TemporaryDirectory",
            ) as temporary_directory,
        ):
            self.module.main()

        print_layout.assert_called_once_with(
            tracks
        )
        extract_track.assert_not_called()
        temporary_directory.assert_not_called()

    def test_concise_command_output_shows_only_progress(self):
        output = io.StringIO()

        with redirect_stdout(output):
            returncode, captured = (
                workflow.run_command_capture(
                    [
                        sys.executable,
                        "-c",
                        (
                            "print('redumper detail'); "
                            "print('[ 42%] LBA: 42/100'); "
                            "print('[100%] LBA: 100/100')"
                        ),
                    ],
                    progress_label="Reading",
                )
            )

        self.assertEqual(returncode, 0)
        self.assertIn(
            "redumper detail",
            captured,
        )
        self.assertNotIn(
            "redumper detail",
            output.getvalue(),
        )
        self.assertIn(
            "Reading: 100%",
            output.getvalue().replace("\r", ""),
        )

    def test_progress_tracks_current_lba(self):
        output = io.StringIO()
        tracks = [
            {
                "number": 1,
                "begin": 0,
                "end": 100,
            },
            {
                "number": 2,
                "begin": 100,
                "end": 200,
            },
        ]

        with redirect_stdout(output):
            returncode, _captured = (
                workflow.run_command_capture(
                    [
                        sys.executable,
                        "-c",
                        (
                            "print('[ 25%] LBA: 50/200'); "
                            "print('[ 50%] LBA: 100/200'); "
                            "print('[100%] LBA: 200/200')"
                        ),
                    ],
                    progress_label="Reading",
                    progress_tracks=tracks,
                )
            )

        rendered = output.getvalue().replace(
            "\r",
            "",
        )
        self.assertEqual(returncode, 0)
        self.assertIn(
            "Reading track 01:  25%",
            rendered,
        )
        self.assertIn(
            "Reading track 02:  50%",
            rendered,
        )
        self.assertIn(
            "Reading track 02: 100%",
            rendered,
        )

    def test_progress_identifies_omitted_data_track(self):
        output = io.StringIO()
        tracks = [
            {
                "number": 1,
                "begin": 0,
                "end": 100,
            },
            {
                "number": 3,
                "begin": 200,
                "end": 300,
            },
        ]

        with redirect_stdout(output):
            returncode, _captured = (
                workflow.run_command_capture(
                    [
                        sys.executable,
                        "-c",
                        (
                            "print('[ 25%] LBA: 50/300'); "
                            "print('[ 50%] LBA: 150/300'); "
                            "print('[ 75%] LBA: 250/300')"
                        ),
                    ],
                    progress_label="Reading",
                    progress_tracks=tracks,
                )
            )

        rendered = output.getvalue().replace(
            "\r",
            "",
        )
        self.assertEqual(returncode, 0)
        self.assertIn(
            "Reading track 01:  25%",
            rendered,
        )
        self.assertIn(
            "Reading data:  50%",
            rendered,
        )
        self.assertIn(
            "Reading track 03:  75%",
            rendered,
        )

    def test_progress_labels_selected_data_track(self):
        label = workflow.format_progress_label(
            "Reading",
            1,
            [
                {
                    "number": 1,
                    "kind": "data",
                }
            ],
        )

        self.assertEqual(
            label,
            "Reading data track 01",
        )

    def test_verbose_command_output_is_unfiltered(self):
        output = io.StringIO()

        with redirect_stdout(output):
            returncode, _captured = (
                workflow.run_command_capture(
                    [
                        sys.executable,
                        "-c",
                        "print('redumper detail')",
                    ],
                    verbose=True,
                    progress_label="Reading",
                )
            )

        self.assertEqual(returncode, 0)
        self.assertIn(
            "redumper detail",
            output.getvalue(),
        )

    def test_quiet_command_output_is_empty(self):
        output = io.StringIO()

        with redirect_stdout(output):
            returncode, captured = (
                workflow.run_command_capture(
                    [
                        sys.executable,
                        "-c",
                        (
                            "print('redumper detail'); "
                            "print('[100%] LBA: 100/100')"
                        ),
                    ]
                )
            )

        self.assertEqual(returncode, 0)
        self.assertIn(
            "redumper detail",
            captured,
        )
        self.assertEqual(
            output.getvalue(),
            "",
        )

    def test_abort_on_skip_policy(self):
        clean = {
            "SCSI": 0,
            "C2": 0,
            "Q": 4,
        }
        unresolved = {
            "SCSI": 1,
            "C2": 2,
            "Q": 4,
        }

        self.assertFalse(
            integrity.should_abort_on_errors(
                clean,
                True,
            )
        )
        self.assertFalse(
            integrity.should_abort_on_errors(
                unresolved,
                False,
            )
        )
        self.assertTrue(
            integrity.should_abort_on_errors(
                unresolved,
                True,
            )
        )

    def test_split_write_offsets_parse_constant_and_shifted(self):
        self.assertEqual(
            integrity.parse_split_write_offsets(
                "disc write offset: +48\n"
            ),
            [(0, 48)],
        )
        self.assertEqual(
            integrity.parse_split_write_offsets(
                "disc write offset: -12\n\n"
                "offset shift correction applied:\n"
                "  LBA:      0, offset: -12\n"
                "  LBA:   1000, offset: +576\n\n"
            ),
            [(0, -12), (1000, 576)],
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "offset-shift boundaries",
        ):
            integrity.parse_split_write_offsets(
                "disc write offset: +48\n"
                "offset shift correction applied:\n"
                "  changed format\n"
            )

    def test_track_media_errors_use_logical_ranges_and_write_offset(self):
        tracks = [
            {
                "number": 1,
                "begin": 10,
                "end": 12,
                "length": 2,
            },
            {
                "number": 2,
                "begin": 12,
                "end": 14,
                "length": 2,
            },
        ]
        offset = 48
        first_sample = (
            (tracks[0]["begin"] - integrity.REDUMPER_LBA_START)
            * integrity.SAMPLES_PER_SECTOR
            + offset
        )
        states = bytearray(
            [integrity.REDUMPER_MAX_STATE]
            * (4 * integrity.SAMPLES_PER_SECTOR)
        )
        states[integrity.SAMPLES_PER_SECTOR] = (
            integrity.REDUMPER_ERROR_SKIP
        )
        states[2 * integrity.SAMPLES_PER_SECTOR] = (
            integrity.REDUMPER_ERROR_C2
        )

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "disc.state"
            with state_path.open("wb") as state_file:
                state_file.seek(first_sample)
                state_file.write(states)

            result = integrity.inspect_track_media_errors(
                state_path,
                tracks,
                [(0, offset)],
            )

        self.assertEqual(result[1]["SCSI"], 1)
        self.assertEqual(result[1]["C2"], 0)
        self.assertEqual(result[1]["SCSI sectors"], 1)
        self.assertEqual(result[2]["SCSI"], 0)
        self.assertEqual(result[2]["C2"], 1)
        self.assertEqual(result[2]["C2 sectors"], 1)

    def make_mmc_toc_response(
        self,
        descriptors,
        first_track=1,
        last_track=None,
    ):
        if last_track is None:
            regular_tracks = [
                number
                for number, _control, _lba
                in descriptors
                if number != 0xAA
            ]
            last_track = max(regular_tracks)

        body = bytearray(
            [first_track, last_track]
        )

        for number, control, lba in descriptors:
            body.extend(
                bytes(
                    [
                        0,
                        0x10 | control,
                        number,
                        0,
                    ]
                )
            )
            body.extend(
                lba.to_bytes(
                    4,
                    byteorder="big",
                )
            )

        return (
            len(body).to_bytes(
                2,
                byteorder="big",
            )
            + body
        )

    def test_parse_mmc_toc_includes_audio_and_data(self):
        response = self.make_mmc_toc_response(
            [
                (1, 0x04, 0),
                (2, 0x00, 100),
                (0xAA, 0x04, 250),
            ]
        )

        tracks = layout.parse_mmc_toc(
            response
        )

        self.assertEqual(
            [track["number"] for track in tracks],
            [1, 2],
        )
        self.assertEqual(
            [track["kind"] for track in tracks],
            ["data", "audio"],
        )
        self.assertEqual(
            [track["length"] for track in tracks],
            [100, 150],
        )
        self.assertEqual(tracks[1]["end"], 250)

    def test_parse_mmc_toc_rejects_truncated_response(self):
        response = self.make_mmc_toc_response(
            [
                (1, 0x00, 0),
                (0xAA, 0x00, 100),
            ]
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "truncated",
        ):
            layout.parse_mmc_toc(
                response[:-1]
            )

    def test_reconcile_disc_layout_preserves_data(self):
        mmc_tracks = [
            {
                "number": 1,
                "kind": "data",
                "control": 4,
                "length": 100,
                "length_msf": "00:01:25",
                "begin": 0,
                "begin_msf": "00:00:00",
                "end": 100,
            },
            {
                "number": 2,
                "kind": "audio",
                "control": 0,
                "length": 150,
                "length_msf": "00:02:00",
                "begin": 100,
                "begin_msf": "00:01:25",
                "end": 250,
            },
        ]
        audio_tracks = [
            {
                "number": 2,
                "length": 150,
                "length_msf": "00:02.00",
                "begin": 100,
                "begin_msf": "00:01.25",
                "end": 250,
            }
        ]

        tracks = layout.reconcile_disc_layout(
            mmc_tracks,
            audio_tracks,
        )

        self.assertEqual(
            [track["kind"] for track in tracks],
            ["data", "audio"],
        )
        self.assertEqual(
            tracks[1]["length_msf"],
            "00:02.00",
        )

    def test_reconcile_disc_layout_rejects_boundary_mismatch(self):
        mmc_tracks = [
            {
                "number": 1,
                "kind": "audio",
                "control": 0,
                "length": 100,
                "length_msf": "00:01:25",
                "begin": 0,
                "begin_msf": "00:00:00",
                "end": 100,
            }
        ]
        audio_tracks = [
            {
                "number": 1,
                "length": 99,
                "length_msf": "00:01.24",
                "begin": 0,
                "begin_msf": "00:00.00",
                "end": 99,
            }
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "boundaries disagree",
        ):
            layout.reconcile_disc_layout(
                mmc_tracks,
                audio_tracks,
            )

    def test_data_only_layout_does_not_call_cdparanoia(self):
        data_tracks = [
            {
                "number": 1,
                "kind": "data",
                "control": 4,
                "length": 100,
                "length_msf": "00:01:25",
                "begin": 0,
                "begin_msf": "00:00:00",
                "end": 100,
            }
        ]

        with (
            mock.patch.object(
                self.workflow,
                "read_mmc_toc",
                return_value=data_tracks,
            ),
            mock.patch.object(
                self.workflow,
                "read_disc_toc",
            ) as read_audio_toc,
        ):
            tracks = self.module.read_disc_layout(
                "/dev/sg-test"
            )

        self.assertEqual(tracks, data_tracks)
        read_audio_toc.assert_not_called()

    def test_print_disc_layout_shows_track_types(self):
        output = io.StringIO()
        tracks = [
            {
                "number": 1,
                "kind": "data",
                "length": 100,
                "begin": 0,
                "end": 100,
            },
            {
                "number": 2,
                "kind": "audio",
                "length": 150,
                "begin": 100,
                "end": 250,
            },
        ]

        with redirect_stdout(output):
            self.module.print_disc_layout(
                tracks
            )

        rendered = output.getvalue()
        self.assertIn("Disc track layout", rendered)
        self.assertIn("data", rendered)
        self.assertIn("audio", rendered)

    def test_track_zero_uses_track_one_begin(self):
        tracks = [
            {
                "number": 1,
                "length": 1000,
                "length_msf": "00:13.25",
                "begin": 300,
                "begin_msf": "00:04.00",
                "end": 1300,
            }
        ]

        track = layout.find_requested_track(
            tracks,
            0,
        )

        self.assertEqual(track["begin"], 0)
        self.assertEqual(track["end"], 300)
        self.assertEqual(track["length"], 300)

    def test_track_zero_rejected_without_pregap(self):
        tracks = [
            {
                "number": 1,
                "length": 1000,
                "length_msf": "00:13.25",
                "begin": 0,
                "begin_msf": "00:00.00",
                "end": 1000,
            }
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "Track 0 does not exist",
        ):
            layout.find_requested_track(
                tracks,
                0,
            )

    def test_track_zero_uses_track_one_bin_before_index01(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            cue_path = workdir / "track00.cue"
            bin_path = workdir / "track01.bin"

            cue_path.write_text(
                'FILE "track01.bin" BINARY\n'
                '  TRACK 01 AUDIO\n'
                '    INDEX 00 00:00:00\n'
                '    INDEX 01 00:04:00\n',
                encoding="utf-8",
            )
            bin_path.write_bytes(
                bytes(301 * outputs.SECTOR_SIZE)
            )

            segments, selected_cue, skipped = (
                cue.identify_generated_audio_segments(
                    workdir,
                    "track00",
                    [cue_path, bin_path],
                    0,
                    300,
                )
            )

            self.assertEqual(selected_cue, cue_path)
            self.assertEqual(skipped, 0)
            self.assertEqual(len(segments), 1)
            self.assertEqual(
                segments[0]["start_sector"],
                0,
            )
            self.assertEqual(
                segments[0]["sectors"],
                300,
            )

    def test_track_zero_rejects_cue_boundary_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            cue_path = workdir / "track00.cue"
            bin_path = workdir / "track01.bin"

            cue_path.write_text(
                'FILE "track01.bin" BINARY\n'
                '  TRACK 01 AUDIO\n'
                '    INDEX 00 00:00:00\n'
                '    INDEX 01 00:04:00\n',
                encoding="utf-8",
            )
            bin_path.write_bytes(
                bytes(301 * outputs.SECTOR_SIZE)
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "expected 299 from cdparanoia",
            ):
                cue.identify_generated_audio_segments(
                    workdir,
                    "track00",
                    [cue_path, bin_path],
                    0,
                    299,
                )

    def make_mode1_raw_sector(self, payload=None):
        if payload is None:
            payload = bytes(
                iso9660.ISO_SECTOR_SIZE
            )

        sector = bytearray(
            outputs.SECTOR_SIZE
        )
        sector[:12] = (
            b"\x00"
            + b"\xff" * 10
            + b"\x00"
        )
        sector[15] = 1
        sector[16:16 + iso9660.ISO_SECTOR_SIZE] = payload
        return bytes(sector)

    def make_iso9660_payloads(self, volume_sectors=20):
        payloads = [
            bytearray(iso9660.ISO_SECTOR_SIZE)
            for _ in range(volume_sectors)
        ]
        primary = payloads[16]
        primary[0] = 1
        primary[1:6] = b"CD001"
        primary[6] = 1
        primary[80:84] = volume_sectors.to_bytes(
            4,
            byteorder="little",
        )
        primary[84:88] = volume_sectors.to_bytes(
            4,
            byteorder="big",
        )
        primary[128:130] = (
            iso9660.ISO_SECTOR_SIZE.to_bytes(
                2,
                byteorder="little",
            )
        )
        primary[130:132] = (
            iso9660.ISO_SECTOR_SIZE.to_bytes(
                2,
                byteorder="big",
            )
        )
        root = primary[156:190]
        root[0] = 34
        root_extent = volume_sectors - 1
        root[2:6] = root_extent.to_bytes(
            4,
            byteorder="little",
        )
        root[6:10] = root_extent.to_bytes(
            4,
            byteorder="big",
        )
        root[10:14] = iso9660.ISO_SECTOR_SIZE.to_bytes(
            4,
            byteorder="little",
        )
        root[14:18] = iso9660.ISO_SECTOR_SIZE.to_bytes(
            4,
            byteorder="big",
        )
        root[25] = 0x02
        root[28:30] = (1).to_bytes(
            2,
            byteorder="little",
        )
        root[30:32] = (1).to_bytes(
            2,
            byteorder="big",
        )
        root[32] = 1
        root[33] = 0
        primary[156:190] = root
        return [bytes(payload) for payload in payloads]

    def test_identify_generated_data_track_uses_index01(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            cue_path = workdir / "track01.cue"
            bin_path = workdir / "track01.bin"
            cue_path.write_text(
                'FILE "track01.bin" BINARY\n'
                '  TRACK 01 MODE1/2352\n'
                '    INDEX 00 00:00:00\n'
                '    INDEX 01 00:00:01\n',
                encoding="utf-8",
            )
            bin_path.write_bytes(
                bytes(3 * outputs.SECTOR_SIZE)
            )

            data_track = (
                cue.identify_generated_data_track(
                    workdir,
                    "track01",
                    [cue_path, bin_path],
                    1,
                )
            )

            self.assertEqual(
                data_track["track_type"],
                "MODE1/2352",
            )
            self.assertEqual(
                data_track["start_sector"],
                1,
            )
            self.assertEqual(
                data_track["sectors"],
                2,
            )

    def test_mode1_data_track_converts_to_trimmed_iso(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            bin_path = workdir / "track01.bin"
            iso_path = workdir / "track01.iso"
            fixture_path = workdir / "fixture.iso"
            generator = shutil.which("genisoimage")

            if generator:
                source_directory = workdir / "source"
                source_directory.mkdir()
                (source_directory / "README.TXT").write_text(
                    "redumper-cdda ISO test\n",
                    encoding="ascii",
                )
                result = subprocess.run(
                    [
                        generator,
                        "-quiet",
                        "-no-pad",
                        "-o",
                        str(fixture_path),
                        str(source_directory),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=result.stderr,
                )
                fixture_bytes = fixture_path.read_bytes()
                self.assertEqual(
                    len(fixture_bytes)
                    % iso9660.ISO_SECTOR_SIZE,
                    0,
                )
                payloads = [
                    fixture_bytes[offset:offset + iso9660.ISO_SECTOR_SIZE]
                    for offset in range(
                        0,
                        len(fixture_bytes),
                        iso9660.ISO_SECTOR_SIZE,
                    )
                ]
            else:
                payloads = self.make_iso9660_payloads(20)
                fixture_bytes = b"".join(payloads)

            bin_path.write_bytes(
                b"".join(
                    self.make_mode1_raw_sector(payload)
                    for payload in payloads
                )
                + self.make_mode1_raw_sector()
            )
            data_track = {
                "path": bin_path,
                "track": 1,
                "track_type": "MODE1/2352",
                "sector_size": outputs.SECTOR_SIZE,
                "start_sector": 0,
                "sectors": len(payloads) + 1,
            }

            iso9660.data_track_to_iso(
                data_track,
                iso_path,
            )

            self.assertEqual(
                iso_path.stat().st_size,
                len(fixture_bytes),
            )
            self.assertEqual(
                iso_path.read_bytes(),
                fixture_bytes,
            )
            with iso_path.open("rb") as iso_file:
                iso_file.seek(
                    16 * iso9660.ISO_SECTOR_SIZE
                )
                self.assertEqual(
                    iso_file.read(6),
                    b"\x01CD001",
                )

            if generator and shutil.which("isoinfo"):
                result = subprocess.run(
                    [
                        "isoinfo",
                        "-d",
                        "-i",
                        str(iso_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=result.stderr,
                )

    def test_mode2_form1_payload_is_supported(self):
        sector = bytearray(
            outputs.SECTOR_SIZE
        )
        sector[:12] = (
            b"\x00"
            + b"\xff" * 10
            + b"\x00"
        )
        sector[15] = 2
        sector[16:20] = b"\x01\x02\x08\x00"
        sector[20:24] = sector[16:20]
        sector[24:24 + iso9660.ISO_SECTOR_SIZE] = (
            b"A" * iso9660.ISO_SECTOR_SIZE
        )

        payload = iso9660.extract_iso_payload(
            bytes(sector),
            "MODE2/2352",
        )

        self.assertEqual(
            payload,
            b"A" * iso9660.ISO_SECTOR_SIZE,
        )

    def test_mode2_form2_payload_is_rejected(self):
        sector = bytearray(
            outputs.SECTOR_SIZE
        )
        sector[:12] = (
            b"\x00"
            + b"\xff" * 10
            + b"\x00"
        )
        sector[15] = 2
        sector[16:20] = b"\x01\x02\x28\x00"
        sector[20:24] = sector[16:20]

        with self.assertRaisesRegex(
            RuntimeError,
            "Form 2",
        ):
            iso9660.extract_iso_payload(
                bytes(sector),
                "MODE2/2352",
            )

    def make_range_tracks(self):
        return [
            {
                "number": number,
                "length": 100,
                "length_msf": "00:01.25",
                "begin": number * 100,
                "begin_msf": "00:00.00",
                "end": (number + 1) * 100,
            }
            for number in range(1, 5)
        ]

    def resolve_numbers(self, value):
        selection = layout.parse_track_selection(
            value
        )
        tracks = layout.resolve_track_selection(
            self.make_range_tracks(),
            selection,
        )
        return [
            track["number"]
            for track in tracks
        ]

    def test_track_range_forms(self):
        self.assertEqual(
            self.resolve_numbers("-"),
            [1, 2, 3, 4],
        )
        self.assertEqual(
            self.resolve_numbers("2"),
            [2],
        )
        self.assertEqual(
            self.resolve_numbers("1-3"),
            [1, 2, 3],
        )
        self.assertEqual(
            self.resolve_numbers("-3"),
            [1, 2, 3],
        )
        self.assertEqual(
            self.resolve_numbers("3-"),
            [3, 4],
        )
        self.assertEqual(
            self.resolve_numbers("0-3"),
            [0, 1, 2, 3],
        )

    def test_open_start_range_excludes_track_zero(self):
        self.assertEqual(
            self.resolve_numbers("-1"),
            [1],
        )

    def test_argparse_accepts_open_start_range(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "-3",
        ]
        parsed = []

        def extract_track(args, _workdir):
            parsed.append(args.track)

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

        self.assertEqual(
            parsed,
            [{"start": None, "end": 3}],
        )

    def test_argparse_accepts_full_disc_range(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "-",
        ]
        parsed = []

        def extract_track(args, _workdir):
            parsed.append(args.track)

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

        self.assertEqual(
            parsed,
            [{"start": None, "end": None}],
        )

    def test_argparse_defaults_to_full_disc(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
        ]
        parsed = []

        def extract_track(args, _workdir):
            parsed.append(args.track)

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

        self.assertEqual(
            parsed,
            [{"start": None, "end": None}],
        )

    def test_accuraterip_defaults_on_when_arver_is_available(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "2",
        ]
        enabled = []

        def extract_track(args, _workdir):
            enabled.append(args.accuraterip)

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "load_accuraterip_library",
                return_value={},
            ) as load_library,
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

        load_library.assert_called_once_with()
        self.assertEqual(enabled, [True])

    def test_missing_arver_disables_accuraterip(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "2",
        ]
        enabled = []

        def extract_track(args, _workdir):
            enabled.append(args.accuraterip)

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "load_accuraterip_library",
                side_effect=RuntimeError("ARver is unavailable"),
            ),
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

        self.assertEqual(enabled, [False])

    def test_no_accuraterip_disables_library_probe(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "2",
            "--no-accuraterip",
        ]
        enabled = []

        def extract_track(args, _workdir):
            enabled.append(args.accuraterip)

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "load_accuraterip_library",
            ) as load_library,
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

        load_library.assert_not_called()
        self.assertEqual(enabled, [False])

    def test_help_explains_track_selection(self):
        output = io.StringIO()
        arguments = [
            str(SCRIPT_PATH),
            "--help",
        ]

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as caught,
        ):
            self.module.main()

        rendered = output.getvalue()

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("TRACK SELECTION:", rendered)
        self.assertIn(
            "N-M    extract Tracks N through M, inclusive",
            rendered,
        )
        self.assertIn(
            "-      extract Track 1 through the final numbered track "
            "(default)",
            rendered,
        )
        self.assertIn(
            "Omitting TRACK is equivalent to '-'",
            rendered,
        )
        self.assertIn(
            "Track 0 is never implicit",
            rendered,
        )
        self.assertIn(
            "Use --include-data",
            rendered,
        )
        self.assertIn("--no-accuraterip", rendered)
        self.assertNotIn("\n  --accuraterip", rendered)
        self.assertIn("-s, --single-file", rendered)
        self.assertIn("--prefix PREFIX", rendered)
        self.assertNotIn("--batch", rendered)

    def test_accuraterip_disc_id_uses_complete_mmc_layout(self):
        tracks = [
            {
                "number": 1,
                "kind": "audio",
                "begin": 0,
                "end": 100,
            },
            {
                "number": 2,
                "kind": "data",
                "begin": 100,
                "end": 250,
            },
        ]
        calls = {}

        def accuraterip_ids(offsets, lead_out):
            calls["audio"] = (offsets, lead_out)
            return "11111111", "22222222"

        def freedb_id(offsets, lead_out):
            calls["all"] = (offsets, lead_out)
            return "33333333"

        disc_id = accuraterip.build_accuraterip_disc_id(
            tracks,
            library={
                "accuraterip_ids": accuraterip_ids,
                "freedb_id": freedb_id,
            },
        )

        self.assertEqual(
            disc_id,
            "001-11111111-22222222-33333333",
        )
        self.assertEqual(
            calls["audio"],
            ([150], 400),
        )
        self.assertEqual(
            calls["all"],
            ([150, 250], 400),
        )

    def test_accuraterip_verifies_selected_track_by_audio_index(self):
        tracks = [
            {
                "number": 1,
                "kind": "audio",
                "length": 1,
                "begin": 0,
                "end": 1,
            },
            {
                "number": 2,
                "kind": "audio",
                "length": 1,
                "begin": 1,
                "end": 2,
            },
        ]
        checksum_calls = []
        fetched_ids = []

        class DatabaseDisc:
            @staticmethod
            def make_dict():
                return {
                    1: {},
                    2: {
                        0x22222222: {
                            "confidence": 9,
                            "response": 3,
                        }
                    },
                    3: {},
                }

        class Fetcher:
            @classmethod
            def from_id(cls, disc_id):
                fetched_ids.append(disc_id)
                return cls()

            @staticmethod
            def fetch():
                return DatabaseDisc()

        def get_checksums(path, track_number, total_tracks):
            checksum_calls.append(
                (
                    Path(path).name,
                    track_number,
                    total_tracks,
                )
            )
            return Namespace(
                arv1=0x11111111,
                arv2=0x22222222,
            )

        library = {
            "get_checksums": get_checksums,
            "fetcher": Fetcher,
            "accuraterip_ids": (
                lambda _offsets, _lead_out: (
                    "aaaaaaaa",
                    "bbbbbbbb",
                )
            ),
            "freedb_id": (
                lambda _offsets, _lead_out: "cccccccc"
            ),
        }

        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            bin_path = workdir / "audio.bin"
            bin_path.write_bytes(
                bytes(2 * outputs.SECTOR_SIZE)
            )
            report = accuraterip.verify_with_accuraterip(
                tracks,
                [
                    {
                        "track": tracks[1],
                        "segments": [
                            {
                                "path": bin_path,
                                "track": 2,
                                "start_sector": 1,
                                "sectors": 1,
                            }
                        ],
                    }
                ],
                workdir,
                library=library,
            )

        self.assertEqual(
            fetched_ids,
            ["002-aaaaaaaa-bbbbbbbb-cccccccc"],
        )
        self.assertEqual(
            checksum_calls,
            [("accuraterip-track02.wav", 2, 2)],
        )
        self.assertEqual(
            report["results"],
            [
                {
                    "track": 2,
                    "status": "verified",
                    "version": "ARv2",
                    "checksum": 0x22222222,
                    "confidence": 9,
                    "response": 3,
                }
            ],
        )

    def test_accuraterip_skips_track_zero(self):
        tracks = [
            {
                "number": 1,
                "kind": "audio",
                "length": 1,
                "begin": 1,
                "end": 2,
            }
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "Track 0 and data tracks are not tracked",
        ):
            accuraterip.verify_with_accuraterip(
                tracks,
                [
                    {
                        "track": {
                            "number": 0,
                            "kind": "audio",
                        },
                        "segments": [],
                    }
                ],
                Path("/tmp"),
                library={},
            )

    def test_accuraterip_falls_back_to_arv1(self):
        result = accuraterip.match_accuraterip_checksums(
            0x11111111,
            0x22222222,
            {
                0x11111111: {
                    "confidence": 7,
                    "response": 2,
                }
            },
        )

        self.assertEqual(
            result,
            {
                "status": "verified",
                "version": "ARv1",
                "checksum": 0x11111111,
                "confidence": 7,
                "response": 2,
            },
        )

    def test_argparse_defaults_to_separate_include_data_files(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "1-3",
            "--include-data",
        ]
        parsed = []

        def extract_track(args, _workdir):
            parsed.append(
                (
                    args.include_data,
                    args.single_file,
                )
            )

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

        self.assertEqual(parsed, [(True, False)])

    def test_argparse_rejects_mixed_range_as_single_file(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "1-3",
            "--include-data",
            "--single-file",
        ]

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            self.module.main()

        self.assertEqual(caught.exception.code, 2)

    def test_combined_range_uses_one_track_wav(self):
        tracks = layout.resolve_track_selection(
            self.make_range_tracks(),
            layout.parse_track_selection("1-3"),
        )

        with tempfile.TemporaryDirectory() as directory:
            jobs = outputs.build_output_jobs(
                tracks,
                True,
                output_directory=directory,
            )

            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                jobs[0]["output_path"].name,
                "track.wav",
            )
            self.assertEqual(
                jobs[0]["expected_sectors"],
                300,
            )

    def test_default_range_uses_track_numbered_wavs(self):
        tracks = layout.resolve_track_selection(
            self.make_range_tracks(),
            layout.parse_track_selection("1-3"),
        )

        with tempfile.TemporaryDirectory() as directory:
            jobs = outputs.build_output_jobs(
                tracks,
                False,
                output_directory=directory,
            )

            self.assertEqual(
                [job["output_path"].name for job in jobs],
                [
                    "track01.wav",
                    "track02.wav",
                    "track03.wav",
                ],
            )
            self.assertEqual(
                [job["expected_sectors"] for job in jobs],
                [100, 100, 100],
            )

    def test_custom_prefix_applies_to_separate_outputs(self):
        tracks = self.make_range_tracks()[:2]
        tracks[1]["kind"] = "data"

        with tempfile.TemporaryDirectory() as directory:
            jobs = outputs.build_output_jobs(
                tracks,
                False,
                output_directory=directory,
                prefix="album-",
            )

        self.assertEqual(
            [job["output_path"].name for job in jobs],
            ["album-01.wav", "album-02.iso"],
        )

    def test_custom_prefix_applies_to_combined_output(self):
        tracks = self.make_range_tracks()[:2]

        with tempfile.TemporaryDirectory() as directory:
            jobs = outputs.build_output_jobs(
                tracks,
                True,
                output_directory=directory,
                prefix="album",
            )

        self.assertEqual(
            jobs[0]["output_path"].name,
            "album.wav",
        )

    def test_short_single_file_and_prefix_options(self):
        arguments = [
            str(SCRIPT_PATH),
            "/dev/sg-test",
            "1-3",
            "-s",
            "-p",
            "album",
        ]
        parsed = []

        def extract_track(args, _workdir):
            parsed.append(
                (args.single_file, args.prefix)
            )

        with (
            mock.patch.object(
                sys,
                "argv",
                arguments,
            ),
            mock.patch.object(
                self.module.shutil,
                "which",
                return_value="/mock/tool",
            ),
            mock.patch.object(
                self.module,
                "extract_track",
                side_effect=extract_track,
            ),
        ):
            self.module.main()

        self.assertEqual(parsed, [(True, "album")])

    def test_include_data_range_selects_audio_and_data(self):
        tracks = self.make_range_tracks()[:3]
        tracks[0]["kind"] = "data"
        tracks[1]["kind"] = "audio"
        tracks[2]["kind"] = "data"

        selected = layout.resolve_disc_selection(
            tracks,
            layout.parse_track_selection("-"),
            include_data=True,
        )

        self.assertEqual(
            [track["number"] for track in selected],
            [1, 2, 3],
        )

    def test_mixed_default_uses_wav_and_iso_names(self):
        tracks = self.make_range_tracks()[:3]
        tracks[0]["kind"] = "data"
        tracks[1]["kind"] = "audio"
        tracks[2]["kind"] = "data"

        with tempfile.TemporaryDirectory() as directory:
            jobs = outputs.build_output_jobs(
                tracks,
                False,
                output_directory=directory,
            )

            self.assertEqual(
                [job["output_path"].name for job in jobs],
                [
                    "track01.iso",
                    "track02.wav",
                    "track03.iso",
                ],
            )
            self.assertEqual(
                [job["kind"] for job in jobs],
                ["data", "audio", "data"],
            )

    def test_single_data_track_uses_iso_name(self):
        track = self.make_range_tracks()[0]
        track["kind"] = "data"

        with tempfile.TemporaryDirectory() as directory:
            jobs = outputs.build_output_jobs(
                [track],
                True,
                output_directory=directory,
            )

            self.assertEqual(
                jobs[0]["output_path"].name,
                "track01.iso",
            )
            self.assertEqual(jobs[0]["kind"], "data")

    def test_include_data_single_file_requires_data_track(self):
        track = self.make_range_tracks()[0]
        track["kind"] = "audio"

        with self.assertRaisesRegex(
            RuntimeError,
            "one explicit data track",
        ):
            layout.validate_data_output_mode(
                [track],
                include_data=True,
                single_file=True,
            )

    def test_output_failure_removes_entire_output_set(self):
        with tempfile.TemporaryDirectory() as directory:
            output_directory = Path(directory)
            jobs = []

            for number in (1, 2):
                output_path = (
                    output_directory
                    / f"track{number:02d}.iso"
                )
                jobs.append(
                    {
                        "kind": "data",
                        "data_track": {"track": number},
                        "output_path": output_path,
                        "temporary_path": output_path.with_name(
                            f".{output_path.name}.part"
                        ),
                    }
                )

            def convert(data_track, path, **_kwargs):
                path.write_bytes(b"partial")

                if data_track["track"] == 2:
                    raise RuntimeError("conversion failed")

            with mock.patch.object(
                self.outputs,
                "data_track_to_iso",
                side_effect=convert,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "conversion failed",
                ):
                    outputs.create_output_files(
                        jobs
                    )

            for job in jobs:
                self.assertFalse(
                    job["temporary_path"].exists()
                )
                self.assertFalse(
                    job["output_path"].exists()
                )

    def test_combined_range_assembles_across_track_bins(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            cue_path = workdir / "tracks01-03.cue"
            cue_lines = []

            for number in range(1, 5):
                bin_name = f"track{number:02d}.bin"
                cue_lines.extend(
                    [
                        f'FILE "{bin_name}" BINARY',
                        f"  TRACK {number:02d} AUDIO",
                        "    INDEX 00 00:00:00",
                        "    INDEX 01 00:00:10",
                    ]
                )
                (workdir / bin_name).write_bytes(
                    bytes(100 * outputs.SECTOR_SIZE)
                )

            cue_path.write_text(
                "\n".join(cue_lines) + "\n",
                encoding="utf-8",
            )

            segments, _cue, skipped = (
                cue.identify_generated_audio_segments(
                    workdir,
                    "tracks01-03",
                    [cue_path],
                    1,
                    300,
                )
            )

            self.assertEqual(skipped, 10)
            self.assertEqual(
                [segment["sectors"] for segment in segments],
                [90, 100, 100, 10],
            )

    def test_separate_range_uses_one_dump_and_one_split(self):
        tracks = self.make_range_tracks()[:2]

        for track in tracks:
            track["kind"] = "audio"

        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            output_jobs = [
                {
                    "track": track,
                    "kind": "audio",
                    "component_tracks": [track],
                    "expected_sectors": track["length"],
                    "output_path": (
                        workdir
                        / f"track{track['number']:02d}.wav"
                    ),
                }
                for track in tracks
            ]
            args = Namespace(
                abort_on_skip=False,
                device="/dev/sg-test",
                include_data=False,
                output=None,
                prefix="track",
                quiet=True,
                refine_passes=3,
                retries=100,
                single_file=False,
                track=layout.parse_track_selection(
                    "1-2"
                ),
                verbose=False,
            )
            clean_output = (
                "media errors:\n"
                "  SCSI: 0\n"
                "  C2: 0\n"
                "  Q: 0\n"
            )

            with (
                mock.patch.object(
                    self.workflow,
                    "read_disc_layout",
                    return_value=tracks,
                ),
                mock.patch.object(
                    self.workflow,
                    "build_output_jobs",
                    return_value=output_jobs,
                ),
                mock.patch.object(
                    self.workflow,
                    "run_command_capture",
                    return_value=(0, clean_output),
                ) as run_capture,
                mock.patch.object(
                    self.workflow,
                    "identify_generated_audio_segments",
                    return_value=([], workdir / "disc.cue", 0),
                ) as identify,
                mock.patch.object(
                    self.outputs,
                    "segments_to_wav",
                    side_effect=lambda _segments, path, _sectors, **_kwargs: (
                        path.write_bytes(b"wav")
                    ),
                ) as write_wav,
            ):
                self.module.extract_track(
                    args,
                    workdir,
                )

            self.assertEqual(run_capture.call_count, 2)
            self.assertEqual(identify.call_count, 2)
            self.assertEqual(write_wav.call_count, 2)

    def test_abort_on_skip_keeps_clean_separate_tracks(self):
        tracks = self.make_range_tracks()[:2]

        for track in tracks:
            track["kind"] = "audio"

        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            output_jobs = outputs.build_output_jobs(
                tracks,
                False,
                output_directory=workdir,
            )
            args = Namespace(
                abort_on_skip=True,
                device="/dev/sg-test",
                include_data=False,
                output=None,
                prefix="track",
                quiet=True,
                refine_passes=0,
                retries=100,
                single_file=False,
                track=layout.parse_track_selection("1-2"),
                verbose=False,
            )
            dirty_output = (
                "media errors:\n"
                "  SCSI: 1 samples\n"
                "  C2: 2 samples\n"
                "  Q: 0\n"
            )
            split_output = "disc write offset: +48\n"
            per_track_errors = {
                1: {
                    "SCSI": 0,
                    "C2": 0,
                    "SCSI sectors": 0,
                    "C2 sectors": 0,
                },
                2: {
                    "SCSI": 1,
                    "C2": 2,
                    "SCSI sectors": 1,
                    "C2 sectors": 1,
                },
            }

            with (
                mock.patch.object(
                    self.workflow,
                    "read_disc_layout",
                    return_value=tracks,
                ),
                mock.patch.object(
                    self.workflow,
                    "build_output_jobs",
                    return_value=output_jobs,
                ),
                mock.patch.object(
                    self.workflow,
                    "run_command_capture",
                    side_effect=[
                        (0, dirty_output),
                        (0, split_output),
                    ],
                ) as run_capture,
                mock.patch.object(
                    self.workflow,
                    "inspect_track_media_errors",
                    return_value=per_track_errors,
                ),
                mock.patch.object(
                    self.workflow,
                    "identify_generated_audio_segments",
                    return_value=([], workdir / "disc.cue", 0),
                ) as identify,
                mock.patch.object(
                    self.outputs,
                    "segments_to_wav",
                    side_effect=lambda _segments, path, _sectors, **_kwargs: (
                        path.write_bytes(b"wav")
                    ),
                ) as write_wav,
                self.assertRaisesRegex(
                    SystemExit,
                    "Clean track files were retained",
                ),
            ):
                self.module.extract_track(
                    args,
                    workdir,
                )

            self.assertEqual(run_capture.call_count, 2)
            identify.assert_called_once()
            write_wav.assert_called_once()
            self.assertEqual(
                (workdir / "track01.wav").read_bytes(),
                b"wav",
            )
            self.assertFalse(
                (workdir / "track02.wav").exists()
            )

    def test_abort_on_skip_single_file_remains_all_or_nothing(self):
        tracks = self.make_range_tracks()[:2]

        for track in tracks:
            track["kind"] = "audio"

        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            output_jobs = outputs.build_output_jobs(
                tracks,
                True,
                output_directory=workdir,
            )
            args = Namespace(
                abort_on_skip=True,
                device="/dev/sg-test",
                include_data=False,
                output=None,
                prefix="track",
                quiet=True,
                refine_passes=0,
                retries=100,
                single_file=True,
                track=layout.parse_track_selection("1-2"),
                verbose=False,
            )
            dirty_output = (
                "media errors:\n"
                "  SCSI: 1 samples\n"
                "  C2: 0 samples\n"
                "  Q: 0\n"
            )

            with (
                mock.patch.object(
                    self.workflow,
                    "read_disc_layout",
                    return_value=tracks,
                ),
                mock.patch.object(
                    self.workflow,
                    "build_output_jobs",
                    return_value=output_jobs,
                ),
                mock.patch.object(
                    self.workflow,
                    "run_command_capture",
                    return_value=(0, dirty_output),
                ) as run_capture,
                mock.patch.object(
                    self.outputs,
                    "segments_to_wav",
                ) as write_wav,
                self.assertRaisesRegex(
                    SystemExit,
                    "--single-file",
                ),
            ):
                self.module.extract_track(
                    args,
                    workdir,
                )

            run_capture.assert_called_once()
            write_wav.assert_not_called()
            self.assertFalse(
                (workdir / "track.wav").exists()
            )

    def test_accuraterip_failure_retains_completed_output(self):
        track = self.make_range_tracks()[0]
        track["kind"] = "audio"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "work"
            workdir.mkdir()
            output_path = root / "track01.wav"
            output_jobs = [
                {
                    "track": track,
                    "kind": "audio",
                    "component_tracks": [track],
                    "expected_sectors": track["length"],
                    "output_path": output_path,
                }
            ]
            args = Namespace(
                abort_on_skip=False,
                accuraterip=True,
                device="/dev/sg-test",
                include_data=False,
                output=None,
                prefix="track",
                quiet=True,
                refine_passes=3,
                retries=100,
                single_file=True,
                track=layout.parse_track_selection("1"),
                verbose=False,
            )
            clean_output = (
                "media errors:\n"
                "  SCSI: 0\n"
                "  C2: 0\n"
                "  Q: 0\n"
            )

            with (
                mock.patch.object(
                    self.workflow,
                    "read_disc_layout",
                    return_value=[track],
                ),
                mock.patch.object(
                    self.workflow,
                    "build_output_jobs",
                    return_value=output_jobs,
                ),
                mock.patch.object(
                    self.workflow,
                    "run_command_capture",
                    return_value=(0, clean_output),
                ),
                mock.patch.object(
                    self.workflow,
                    "identify_generated_audio_segments",
                    return_value=([], workdir / "disc.cue", 0),
                ),
                mock.patch.object(
                    self.outputs,
                    "segments_to_wav",
                    side_effect=(
                        lambda _segments, path, _sectors, **_kwargs: (
                            path.write_bytes(b"wav")
                        )
                    ),
                ),
                mock.patch.object(
                    self.workflow,
                    "verify_with_accuraterip",
                    side_effect=RuntimeError("lookup failed"),
                ),
                self.assertRaisesRegex(
                    SystemExit,
                    "Completed output files were retained",
                ),
            ):
                self.module.extract_track(
                    args,
                    workdir,
                )

            self.assertEqual(
                output_path.read_bytes(),
                b"wav",
            )

    def test_mixed_output_uses_one_dump_and_transactional_files(self):
        tracks = self.make_range_tracks()[:2]
        tracks[0]["kind"] = "data"
        tracks[1]["kind"] = "audio"

        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            output_jobs = outputs.build_output_jobs(
                tracks,
                False,
                output_directory=workdir,
            )
            args = Namespace(
                abort_on_skip=False,
                device="/dev/sg-test",
                include_data=True,
                output=None,
                prefix="track",
                quiet=True,
                refine_passes=3,
                retries=100,
                single_file=False,
                track=layout.parse_track_selection(
                    "1-2"
                ),
                verbose=False,
            )
            clean_output = (
                "media errors:\n"
                "  SCSI: 0\n"
                "  C2: 0\n"
                "  Q: 0\n"
            )
            data_track = {
                "cue_path": workdir / "disc.cue",
                "path": workdir / "track01.bin",
                "track": 1,
                "track_type": "MODE1/2352",
                "sector_size": outputs.SECTOR_SIZE,
                "start_sector": 0,
                "sectors": 20,
            }

            with (
                mock.patch.object(
                    self.workflow,
                    "read_disc_layout",
                    return_value=tracks,
                ),
                mock.patch.object(
                    self.workflow,
                    "build_output_jobs",
                    return_value=output_jobs,
                ),
                mock.patch.object(
                    self.workflow,
                    "run_command_capture",
                    return_value=(0, clean_output),
                ) as run_capture,
                mock.patch.object(
                    self.workflow,
                    "identify_generated_data_track",
                    return_value=data_track,
                ) as identify_data,
                mock.patch.object(
                    self.workflow,
                    "identify_generated_audio_segments",
                    return_value=([], workdir / "disc.cue", 0),
                ) as identify_audio,
                mock.patch.object(
                    self.outputs,
                    "data_track_to_iso",
                    side_effect=lambda _track, path, **_kwargs: (
                        path.write_bytes(b"iso")
                    ),
                ) as write_iso,
                mock.patch.object(
                    self.outputs,
                    "segments_to_wav",
                    side_effect=lambda _segments, path, _sectors, **_kwargs: (
                        path.write_bytes(b"wav")
                    ),
                ) as write_wav,
            ):
                self.module.extract_track(
                    args,
                    workdir,
                )

            self.assertEqual(run_capture.call_count, 2)
            self.assertIn(
                "--filesystem-trim",
                run_capture.call_args_list[1].args[0],
            )
            identify_data.assert_called_once()
            identify_audio.assert_called_once()
            write_iso.assert_called_once()
            write_wav.assert_called_once()
            self.assertEqual(
                (workdir / "track01.iso").read_bytes(),
                b"iso",
            )
            self.assertEqual(
                (workdir / "track02.wav").read_bytes(),
                b"wav",
            )

    def test_open_range_omits_data_tracks(self):
        tracks = self.make_range_tracks()[1:3]

        selected = layout.resolve_track_selection(
            tracks,
            layout.parse_track_selection("-3"),
        )

        self.assertEqual(
            [track["number"] for track in selected],
            [2, 3],
        )

    def test_bounded_range_rejects_data_tracks(self):
        tracks = self.make_range_tracks()[1:3]

        with self.assertRaisesRegex(
            RuntimeError,
            "Audio track 1 was not found",
        ):
            layout.resolve_track_selection(
                tracks,
                layout.parse_track_selection("1-3"),
            )

    def test_sigterm_stops_child_and_removes_workspace(self):
        workspaces = []
        previous_handler = signal.getsignal(
            signal.SIGTERM
        )

        def extract_track(_args, workdir):
            workspaces.append(workdir)
            (workdir / "intermediate").write_bytes(
                b"data"
            )

            timer = threading.Timer(
                0.1,
                os.kill,
                args=(
                    os.getpid(),
                    signal.SIGTERM,
                ),
            )
            timer.start()

            try:
                workflow.run_command(
                    [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(30)",
                    ]
                )
            finally:
                timer.cancel()

        try:
            self.module.install_signal_handlers()

            with self.assertRaises(
                self.module.TerminationRequested
            ):
                self.run_main(
                    extract_track,
                )
        finally:
            signal.signal(
                signal.SIGTERM,
                previous_handler,
            )

        self.assertFalse(
            workspaces[0].exists()
        )


class CliCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script()

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

    def test_output_job_mixed_single_file_and_selected_report(self):
        audio = self.audio_track()
        data = dict(audio, number=2, kind="data")
        with self.assertRaisesRegex(RuntimeError, "Data tracks can only"):
            outputs.build_output_jobs([audio, data], True)
        with redirect_stdout(io.StringIO()) as output:
            workflow.print_selected_track(audio)
        self.assertIn("Selected track", output.getvalue())

    def test_main_validation_and_missing_tools(self):
        cases = [
            (["dev", "--retries=-1"], "invalid retry"),
            (["dev", "--refine-passes=-1"], "invalid refine"),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                with (
                    mock.patch.object(sys, "argv", ["cmd", *arguments]),
                    self.assertRaisesRegex(SystemExit, message),
                ):
                    self.module.main()

        for prefix in ("", ".", "../bad"):
            with self.subTest(prefix=prefix):
                with (
                    mock.patch.object(sys, "argv", ["cmd", "dev", "--prefix", prefix]),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    self.module.main()

        for missing, message in (("cdparanoia", "cdparanoia"), ("sg_raw", "sg_raw"), ("redumper", "redumper")):
            with self.subTest(missing=missing):
                def which(name, missing=missing):
                    return None if name == missing else "/mock/tool"

                with (
                    mock.patch.object(sys, "argv", ["cmd", "dev", "--no-accuraterip"]),
                    mock.patch.object(self.module.shutil, "which", side_effect=which),
                    self.assertRaisesRegex(SystemExit, message),
                ):
                    self.module.main()

    def test_main_layout_failure_output_error_and_verbose_workspace(self):
        with (
            mock.patch.object(sys, "argv", ["cmd", "dev", "--show-layout"]),
            mock.patch.object(self.module.shutil, "which", return_value="/mock/tool"),
            mock.patch.object(self.module, "read_disc_layout", side_effect=RuntimeError("bad toc")),
            self.assertRaisesRegex(SystemExit, "bad toc"),
        ):
            self.module.main()

        with (
            mock.patch.object(sys, "argv", ["cmd", "dev", "--output", "x.wav"]),
            mock.patch.object(self.module.shutil, "which", return_value="/mock/tool"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            self.module.main()

        with (
            mock.patch.object(sys, "argv", ["cmd", "dev", "-v", "--no-accuraterip"]),
            mock.patch.object(self.module.shutil, "which", return_value="/mock/tool"),
            mock.patch.object(self.module, "extract_track") as extract,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.module.main()
        self.assertTrue(extract.called)
        self.assertIn("Temporary workspace:", output.getvalue())
        self.assertIn("workspace removed", output.getvalue())

    def test_main_quiet_layout_does_not_print(self):
        with (
            mock.patch.object(sys, "argv", ["cmd", "dev", "--show-layout", "--quiet"]),
            mock.patch.object(self.module.shutil, "which", return_value="/mock/tool"),
            mock.patch.object(self.module, "read_disc_layout", return_value=[]),
            mock.patch.object(self.module, "print_disc_layout") as print_layout,
        ):
            self.module.main()
        print_layout.assert_not_called()


class CliSignalCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script()

    def test_run_translates_interrupts(self):
        with (
            mock.patch.object(self.module, "install_signal_handlers"),
            mock.patch.object(self.module, "main", side_effect=KeyboardInterrupt),
            self.assertRaisesRegex(SystemExit, "Interrupted"),
        ):
            self.module.run()

        termination = self.module.TerminationRequested(self.module.signal.SIGTERM)
        with (
            mock.patch.object(self.module, "install_signal_handlers"),
            mock.patch.object(self.module, "main", side_effect=termination),
            self.assertRaisesRegex(SystemExit, "SIGTERM"),
        ):
            self.module.run()

    def test_signal_handler_skips_unavailable_signal(self):
        class FakeSignal:
            SIGTERM = 15

            def __init__(self):
                self.signal = mock.Mock()

        fake_signal = FakeSignal()
        with mock.patch.object(self.module, "signal", fake_signal):
            self.module.install_signal_handlers()
        self.assertEqual(fake_signal.signal.call_count, 1)


class PackageEntrypointCoverageTests(unittest.TestCase):
    def test_package_and_module_entrypoints(self):
        source = str(Path(__file__).parent.parent / "src")
        sys.path.insert(0, source)
        try:
            package = importlib.import_module("redumper_cdda")
            self.assertEqual(package.__version__, "0.1.0")
            with mock.patch("redumper_cdda.cli.run") as run:
                runpy.run_module("redumper_cdda.__main__", run_name="not_main")
                run.assert_not_called()
                runpy.run_module("redumper_cdda.__main__", run_name="__main__")
                run.assert_called_once_with()
        finally:
            sys.path.remove(source)

    def test_cli_file_main_guard(self):
        source = str(Path(__file__).parent.parent / "src")
        with (
            mock.patch.object(sys, "argv", ["redumper_cdda.cli", "--help"]),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            sys.path.insert(0, source)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    runpy.run_module("redumper_cdda.cli", run_name="__main__")
            finally:
                sys.path.remove(source)
        self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
