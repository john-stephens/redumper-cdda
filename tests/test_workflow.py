"""Tests for process control and extraction orchestration."""

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

from redumper_cdda import workflow


class ProcessCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.reload(workflow)

    def test_stop_process_variants(self):
        stopped = mock.Mock()
        stopped.poll.return_value = 0
        self.module.stop_process(stopped)
        stopped.terminate.assert_not_called()

        missing = mock.Mock()
        missing.poll.return_value = None
        missing.terminate.side_effect = ProcessLookupError
        self.module.stop_process(missing)
        missing.wait.assert_not_called()

        stubborn = mock.Mock()
        stubborn.poll.return_value = None
        stubborn.wait.side_effect = [subprocess.TimeoutExpired("x", 5), 0]
        self.module.stop_process(stubborn)
        stubborn.kill.assert_called_once_with()

    def test_wait_for_process_stops_on_exception(self):
        process = mock.Mock()
        process.wait.side_effect = RuntimeError("wait failed")
        with (
            mock.patch.object(self.module, "stop_process") as stop,
            self.assertRaisesRegex(RuntimeError, "wait failed"),
        ):
            self.module.wait_for_process(process)
        stop.assert_called_once_with(process)

    def test_track_number_for_empty_layout(self):
        self.assertIsNone(self.module.track_number_for_lba([], 10))
        self.assertIsNone(
            self.module.track_number_for_lba(
                [{"number": 1, "begin": 10, "end": 20},
                 {"number": 2, "begin": 30, "end": 40}],
                25,
            )
        )
        self.assertEqual(
            self.module.track_number_for_lba(
                [{"number": 1, "begin": 10, "end": 20}], 0
            ),
            1,
        )

    def test_run_command_capture_stops_on_stream_exception(self):
        class BrokenStream:
            def __iter__(self):
                raise RuntimeError("stream failed")

            def close(self):
                pass

        process = mock.Mock(stdout=BrokenStream())
        with (
            mock.patch.object(self.module.subprocess, "Popen", return_value=process),
            mock.patch.object(self.module, "stop_process") as stop,
            self.assertRaisesRegex(RuntimeError, "stream failed"),
        ):
            self.module.run_command_capture(["tool"])
        stop.assert_called_once_with(process)

    def test_progress_ignores_duplicate_percentage(self):
        with redirect_stdout(io.StringIO()) as output:
            self.module.run_command_capture(
                [sys.executable, "-c", "print('[  0%]'); print('[  0%]')"],
                progress_label="Reading",
            )
        self.assertEqual(output.getvalue().count("Reading:   0%"), 1)

    def test_run_command_success_and_failure_modes(self):
        verbose_process = mock.Mock()
        with (
            mock.patch.object(self.module.subprocess, "Popen", return_value=verbose_process),
            mock.patch.object(self.module, "wait_for_process", return_value=0),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = self.module.run_command(["tool", "arg"], verbose=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("+ tool arg", output.getvalue())

        quiet_process = mock.Mock(returncode=0)
        with (
            mock.patch.object(self.module.subprocess, "Popen", return_value=quiet_process),
            mock.patch.object(
                self.module,
                "communicate_with_process",
                return_value=("captured", None),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = self.module.run_command(["tool"], status_label="Working")
        self.assertEqual(result.stdout, "captured")
        self.assertIn("Working... done", output.getvalue())

        failed_process = mock.Mock(returncode=3)
        with (
            mock.patch.object(self.module.subprocess, "Popen", return_value=failed_process),
            mock.patch.object(
                self.module,
                "communicate_with_process",
                return_value=("bad", None),
            ),
            redirect_stdout(io.StringIO()) as output,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            self.module.run_command(["tool"], status_label="Working")
        self.assertIn("failed", output.getvalue())

        no_status_process = mock.Mock(returncode=0)
        with (
            mock.patch.object(self.module.subprocess, "Popen", return_value=no_status_process),
            mock.patch.object(
                self.module, "communicate_with_process", return_value=("", None)
            ),
        ):
            self.module.run_command(["tool"])


class WorkflowCoverageTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.reload(workflow)

    @staticmethod
    def track(kind="audio", number=1):
        return {
            "number": number,
            "kind": kind,
            "control": 0 if kind == "audio" else 4,
            "begin": 0,
            "end": 2,
            "length": 2,
            "length_msf": "00:00.02",
            "begin_msf": "00:00.00",
        }

    def args(self, **changes):
        values = {
            "device": "/dev/test",
            "track": {"start": 1, "end": 1},
            "include_data": False,
            "single_file": False,
            "output": None,
            "prefix": "track",
            "retries": 1,
            "refine_passes": 0,
            "abort_on_skip": False,
            "verbose": False,
            "quiet": True,
            "accuraterip": False,
        }
        values.update(changes)
        return Namespace(**values)

    def test_extraction_plan_keeps_one_padded_range_for_all_commands(self):
        first = dict(self.track(number=1), begin=100, end=102)
        second = dict(
            self.track(kind="data", number=2),
            begin=102,
            end=105,
            length=3,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                self.module,
                "build_output_jobs",
                return_value=[{"output_path": root / "track01.wav"}],
            ):
                plan = self.module.build_extraction_plan(
                    self.args(include_data=True),
                    root,
                    [first, second],
                    [first, second],
                )

        self.assertEqual(plan.logical_start_lba, 100)
        self.assertEqual(plan.logical_end_lba, 105)
        self.assertEqual(plan.dump_start_lba, 100)
        self.assertEqual(plan.dump_end_lba, 106)
        self.assertEqual(plan.expected_sectors, 5)
        self.assertIn("--lba-start=100", plan.dump_command)
        self.assertIn("--lba-end=106", plan.dump_command)
        self.assertIn("--lba-start=100", plan.refine_command)
        self.assertIn("--lba-end=106", plan.refine_command)
        self.assertIn("--filesystem-trim", plan.split_command)
        self.assertEqual(
            plan.output_jobs[0]["temporary_path"].name,
            ".track01.wav.part",
        )

    def invoke(
        self,
        workdir,
        *,
        args=None,
        tracks=None,
        layout_tracks=None,
        command_results=None,
        parse_errors=None,
        inspect_errors=None,
        create_side_effect=None,
        identify_side_effect=None,
        verify_side_effect=None,
        output_exists=False,
    ):
        args = args or self.args()
        tracks = tracks or [self.track()]
        layout_tracks = layout_tracks or tracks
        output_path = workdir / "track01.wav"
        temporary_path = workdir / ".track01.wav.part"
        if output_exists:
            output_path.write_bytes(b"old")
            temporary_path.write_bytes(b"old part")
        if len(tracks) > 1 and not args.single_file:
            jobs = [
                {
                    "track": track,
                    "kind": track["kind"],
                    "component_tracks": [track] if track["kind"] == "audio" else [],
                    "expected_sectors": track["length"],
                    "output_path": workdir / f"track{track['number']:02d}.wav",
                }
                for track in tracks
            ]
        else:
            jobs = [{
                "track": tracks[0] if len(tracks) == 1 else None,
                "kind": tracks[0]["kind"],
                "component_tracks": tracks if tracks[0]["kind"] == "audio" else [],
                "expected_sectors": sum(track["length"] for track in tracks),
                "output_path": output_path,
            }]
        command_results = command_results or [
            (0, "media errors: SCSI: 0 C2: 0 Q: 0"),
            (0, "disc write offset: 0"),
        ]

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    self.module, "read_disc_layout", return_value=layout_tracks
                )
            )
            stack.enter_context(
                mock.patch.object(self.module, "resolve_disc_selection", return_value=tracks)
            )
            stack.enter_context(
                mock.patch.object(self.module, "build_output_jobs", return_value=jobs)
            )
            stack.enter_context(
                mock.patch.object(
                    self.module, "run_command_capture", side_effect=command_results
                )
            )
            if parse_errors is not None:
                stack.enter_context(
                    mock.patch.object(
                        self.module, "parse_media_errors", side_effect=parse_errors
                    )
                )
            stack.enter_context(mock.patch.object(self.module, "snapshot_files", return_value={}))
            stack.enter_context(mock.patch.object(self.module, "changed_files", return_value=[]))
            stack.enter_context(
                mock.patch.object(
                    self.module,
                    "inspect_track_media_errors",
                    return_value=inspect_errors or {},
                )
            )
            audio_result = ([{"path": workdir / "audio.bin", "track": 1,
                              "start_sector": 0, "sectors": 2, "bin_sectors": 2}],
                            workdir / "disc.cue", 0)
            stack.enter_context(
                mock.patch.object(
                    self.module,
                    "identify_generated_audio_segments",
                    side_effect=identify_side_effect,
                    return_value=audio_result,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.module,
                    "identify_generated_data_track",
                    side_effect=identify_side_effect,
                    return_value={
                        "cue_path": workdir / "disc.cue",
                        "start_sector": 0,
                    },
                )
            )
            create = stack.enter_context(
                mock.patch.object(
                    self.module, "create_output_files", side_effect=create_side_effect
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.module,
                    "verify_with_accuraterip",
                    side_effect=verify_side_effect,
                    return_value={"disc_id": "id", "results": []},
                )
            )
            stack.enter_context(mock.patch.object(self.module, "print_accuraterip_report"))
            self.module.extract_track(args, workdir)
        return create, output_path, temporary_path

    def test_verbose_success_covers_diagnostics_and_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()) as output:
                self.invoke(
                    Path(directory),
                    args=self.args(verbose=True, quiet=False, accuraterip=True),
                    output_exists=True,
                )
        rendered = output.getvalue()
        self.assertIn("Initial partial dump", rendered)
        self.assertIn("Data integrity", rendered)
        self.assertIn("Complete", rendered)
        self.assertIn("Integrity:          PASS", rendered)

    def test_verbose_refine_with_unresolved_errors(self):
        error_text = "media errors: SCSI: 1 C2: 0 Q: 2"
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()) as output:
                self.invoke(
                    Path(directory),
                    args=self.args(verbose=True, quiet=False, refine_passes=1),
                    command_results=[
                        (0, error_text),
                        (0, error_text),
                        (0, "split"),
                    ],
                )
        rendered = output.getvalue()
        self.assertIn("Refine pass 1/1", rendered)
        self.assertIn("WARNING: unresolved", rendered)
        self.assertIn("Integrity:          WARNING", rendered)

    def test_refine_can_clear_errors_and_concise_warning_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                self.invoke(
                    Path(directory),
                    args=self.args(verbose=True, refine_passes=2),
                    command_results=[
                        (0, "media errors: SCSI: 1 C2: 0 Q: 0"),
                        (0, "media errors: SCSI: 0 C2: 0 Q: 0"),
                        (0, "split"),
                    ],
                )
            with redirect_stdout(io.StringIO()) as output:
                self.invoke(
                    Path(directory),
                    args=self.args(quiet=False, refine_passes=0),
                    command_results=[
                        (0, "media errors: SCSI: 1 C2: 0 Q: 0"),
                        (0, "split"),
                    ],
                )
            self.assertIn("Warning: writing output", output.getvalue())

            self.invoke(
                Path(directory),
                args=self.args(refine_passes=1),
                command_results=[
                    (0, "media errors: SCSI: 1 C2: 0 Q: 0"),
                    (0, "media errors: SCSI: 0 C2: 0 Q: 0"),
                    (0, "split"),
                ],
            )

    def test_combined_tracks_keep_first_pregap(self):
        first = self.track(number=1)
        second = dict(self.track(number=2), begin=2, end=4)
        with tempfile.TemporaryDirectory() as directory:
            self.invoke(
                Path(directory),
                args=self.args(single_file=True),
                tracks=[first, second],
            )

    def test_concise_accuraterip_reporting_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()) as output:
                self.invoke(
                    Path(directory),
                    args=self.args(accuraterip=True, quiet=False),
                )
            self.assertIn("Checking AccurateRip", output.getvalue())

            self.invoke(
                Path(directory),
                args=self.args(accuraterip=True, quiet=True),
            )

    def test_concise_single_data_success(self):
        data = self.track("data")
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()) as output:
                self.invoke(
                    Path(directory),
                    args=self.args(
                        include_data=True, single_file=True, quiet=False
                    ),
                    tracks=[data],
                )
        self.assertIn("Writing ISO", output.getvalue())
        self.assertIn("Done.", output.getvalue())

    def test_initial_validation_and_command_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(
                    self.module, "read_disc_layout", side_effect=RuntimeError("layout")
                ),
                self.assertRaisesRegex(SystemExit, "layout"),
            ):
                self.module.extract_track(self.args(), root)

            data = self.track("data")
            with self.assertRaisesRegex(SystemExit, "at least one audio"):
                self.invoke(
                    root,
                    args=self.args(accuraterip=True, include_data=True),
                    tracks=[data],
                )

            audio = self.track("audio", 1)
            selected_data = self.track("data", 2)
            with self.assertRaisesRegex(SystemExit, "no AccurateRip-verifiable"):
                self.invoke(
                    root,
                    args=self.args(accuraterip=True, include_data=True),
                    tracks=[selected_data],
                    layout_tracks=[audio, selected_data],
                )

            for results, parse_errors, message in (
                ([(1, "")], None, "dump failed"),
                ([(0, "")], [None], "Could not determine"),
                ([(0, "errors"), (1, "")], [{"SCSI": 1, "C2": 0, "Q": 0}], "refine failed"),
                (
                    [(0, "errors"), (0, "")],
                    [{"SCSI": 1, "C2": 0, "Q": 0}, None],
                    "after refine",
                ),
                (
                    [(0, "media errors: SCSI: 0 C2: 0 Q: 0"), (1, "")],
                    None,
                    "could not split",
                ),
            ):
                with self.subTest(message=message):
                    with self.assertRaisesRegex(SystemExit, message):
                        self.invoke(
                            root,
                            args=self.args(refine_passes=1),
                            command_results=results,
                            parse_errors=parse_errors,
                        )

    def test_split_error_classification_and_all_skipped(self):
        error = {"SCSI": 1, "C2": 0, "Q": 0}
        command_results = [
            (0, "media errors: SCSI: 1 C2: 0 Q: 0"),
            (0, "bad split output"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SystemExit, "safely identify"):
                self.invoke(
                    root,
                    args=self.args(abort_on_skip=True),
                    command_results=command_results,
                )

            per_track = {
                1: {
                    "SCSI": 1, "C2": 0,
                    "SCSI sectors": 1, "C2 sectors": 0,
                }
            }
            with (
                redirect_stdout(io.StringIO()) as output,
                self.assertRaisesRegex(SystemExit, "every selected track"),
            ):
                self.invoke(
                    root,
                    args=self.args(abort_on_skip=True, quiet=False),
                    command_results=[
                        (0, "media errors: SCSI: 1 C2: 0 Q: 0"),
                        (0, "disc write offset: 0"),
                    ],
                    inspect_errors=per_track,
                )
            self.assertIn("Skipping Track 01", output.getvalue())

            first = self.track(number=1)
            second = dict(self.track(number=2), begin=2, end=4)
            per_track = {
                1: {
                    "SCSI": 1, "C2": 0,
                    "SCSI sectors": 1, "C2 sectors": 0,
                },
                2: {
                    "SCSI": 0, "C2": 0,
                    "SCSI sectors": 0, "C2 sectors": 0,
                },
            }
            with (
                redirect_stdout(io.StringIO()) as output,
                self.assertRaisesRegex(SystemExit, "Clean track files were retained"),
            ):
                self.invoke(
                    root,
                    args=self.args(abort_on_skip=True, quiet=False),
                    tracks=[first, second],
                    command_results=[
                        (0, "media errors: SCSI: 1 C2: 0 Q: 0"),
                        (0, "disc write offset: 0"),
                    ],
                    inspect_errors=per_track,
                )
            self.assertIn("Done with 1 track omitted", output.getvalue())

    def test_conversion_and_verification_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SystemExit, "No output file"):
                self.invoke(root, identify_side_effect=RuntimeError("bad cue"))

            with (
                redirect_stdout(io.StringIO()) as output,
                self.assertRaisesRegex(SystemExit, "output creation failed"),
            ):
                self.invoke(
                    root,
                    args=self.args(quiet=False),
                    create_side_effect=RuntimeError("conversion"),
                )
            self.assertIn("failed", output.getvalue())

            with self.assertRaises(KeyboardInterrupt):
                self.invoke(root, create_side_effect=KeyboardInterrupt())

            with self.assertRaisesRegex(SystemExit, "Completed output files were retained"):
                self.invoke(
                    root,
                    args=self.args(accuraterip=True),
                    verify_side_effect=RuntimeError("network"),
                )
