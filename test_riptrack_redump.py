import importlib.machinery
import importlib.util
import io
import os
import signal
import sys
import tempfile
import threading
import unittest
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
                side_effect=lambda command: (
                    "/mock/cdparanoia"
                    if command == "cdparanoia"
                    else None
                ),
            ),
            mock.patch.object(
                self.module,
                "read_disc_toc",
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
