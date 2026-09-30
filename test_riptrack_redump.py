import importlib.machinery
import importlib.util
import io
import os
import signal
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name(
    "riptrack-redump"
)


def load_script():
    loader = importlib.machinery.SourceFileLoader(
        "riptrack_redump",
        str(SCRIPT_PATH),
    )
    spec = importlib.util.spec_from_loader(
        loader.name,
        loader,
    )
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TemporaryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.module = load_script()

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
                self.module.run_command_capture(
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
                self.module.run_command_capture(
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
                self.module.run_command_capture(
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

    def test_verbose_command_output_is_unfiltered(self):
        output = io.StringIO()

        with redirect_stdout(output):
            returncode, _captured = (
                self.module.run_command_capture(
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
                self.module.run_command_capture(
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
            self.module.should_abort_on_errors(
                clean,
                True,
            )
        )
        self.assertFalse(
            self.module.should_abort_on_errors(
                unresolved,
                False,
            )
        )
        self.assertTrue(
            self.module.should_abort_on_errors(
                unresolved,
                True,
            )
        )

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

        tracks = self.module.parse_mmc_toc(
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
            self.module.parse_mmc_toc(
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

        tracks = self.module.reconcile_disc_layout(
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
            self.module.reconcile_disc_layout(
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
                self.module,
                "read_mmc_toc",
                return_value=data_tracks,
            ),
            mock.patch.object(
                self.module,
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

        track = self.module.find_requested_track(
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
            self.module.find_requested_track(
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
                bytes(301 * self.module.SECTOR_SIZE)
            )

            segments, selected_cue, skipped = (
                self.module.identify_generated_audio_segments(
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
                bytes(301 * self.module.SECTOR_SIZE)
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "expected 299 from cdparanoia",
            ):
                self.module.identify_generated_audio_segments(
                    workdir,
                    "track00",
                    [cue_path, bin_path],
                    0,
                    299,
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
        selection = self.module.parse_track_selection(
            value
        )
        tracks = self.module.resolve_track_selection(
            self.make_range_tracks(),
            selection,
        )
        return [
            track["number"]
            for track in tracks
        ]

    def test_track_range_forms(self):
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

    def test_combined_range_uses_one_track_wav(self):
        tracks = self.module.resolve_track_selection(
            self.make_range_tracks(),
            self.module.parse_track_selection("1-3"),
        )

        with tempfile.TemporaryDirectory() as directory:
            jobs = self.module.build_output_jobs(
                tracks,
                False,
                output_directory=directory,
            )

            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                jobs[0]["wav_path"].name,
                "track.wav",
            )
            self.assertEqual(
                jobs[0]["expected_sectors"],
                300,
            )

    def test_batch_range_uses_track_numbered_wavs(self):
        tracks = self.module.resolve_track_selection(
            self.make_range_tracks(),
            self.module.parse_track_selection("1-3"),
        )

        with tempfile.TemporaryDirectory() as directory:
            jobs = self.module.build_output_jobs(
                tracks,
                True,
                output_directory=directory,
            )

            self.assertEqual(
                [job["wav_path"].name for job in jobs],
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
                    bytes(100 * self.module.SECTOR_SIZE)
                )

            cue_path.write_text(
                "\n".join(cue_lines) + "\n",
                encoding="utf-8",
            )

            segments, _cue, skipped = (
                self.module.identify_generated_audio_segments(
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

    def test_batch_range_uses_one_dump_and_one_split(self):
        tracks = self.make_range_tracks()[:2]

        for track in tracks:
            track["kind"] = "audio"

        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)
            output_jobs = [
                {
                    "track": track,
                    "component_tracks": [track],
                    "expected_sectors": track["length"],
                    "wav_path": (
                        workdir
                        / f"track{track['number']:02d}.wav"
                    ),
                }
                for track in tracks
            ]
            args = Namespace(
                abort_on_skip=False,
                batch=True,
                device="/dev/sg-test",
                output=None,
                quiet=True,
                refine_passes=3,
                retries=100,
                track=self.module.parse_track_selection(
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
                    self.module,
                    "read_disc_layout",
                    return_value=tracks,
                ),
                mock.patch.object(
                    self.module,
                    "build_output_jobs",
                    return_value=output_jobs,
                ),
                mock.patch.object(
                    self.module,
                    "run_command_capture",
                    return_value=(0, clean_output),
                ) as run_capture,
                mock.patch.object(
                    self.module,
                    "run_command",
                ) as run_split,
                mock.patch.object(
                    self.module,
                    "identify_generated_audio_segments",
                    return_value=([], workdir / "disc.cue", 0),
                ) as identify,
                mock.patch.object(
                    self.module,
                    "segments_to_wav",
                ) as write_wav,
            ):
                self.module.extract_track(
                    args,
                    workdir,
                )

            self.assertEqual(run_capture.call_count, 1)
            self.assertEqual(run_split.call_count, 1)
            self.assertEqual(identify.call_count, 2)
            self.assertEqual(write_wav.call_count, 2)

    def test_open_range_omits_data_tracks(self):
        tracks = self.make_range_tracks()[1:3]

        selected = self.module.resolve_track_selection(
            tracks,
            self.module.parse_track_selection("-3"),
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
            self.module.resolve_track_selection(
                tracks,
                self.module.parse_track_selection("1-3"),
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
                self.module.run_command(
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


if __name__ == "__main__":
    unittest.main()
