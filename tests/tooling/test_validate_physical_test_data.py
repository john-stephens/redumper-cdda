import hashlib
import io
import json
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import validate_physical_test_data as validate


def synthetic_manifest(profile="synthetic"):
    return {
        "format": 1,
        "status": "complete",
        "profile": profile,
        "layout": {
            "tracks": [
                {
                    "number": 1,
                    "kind": "audio",
                    "begin_lba": 10,
                    "length_sectors": 2,
                },
                {
                    "number": 2,
                    "kind": "data",
                    "begin_lba": 12,
                    "length_sectors": 20,
                },
            ]
        },
        "scenarios": [
            {
                "name": "case",
                "status": "complete",
                "selected_tracks": [1],
                "include_data": False,
                "expected_media_errors": False,
                "existing_dump": "case/disc",
                "cdparanoia_toc_file": "toc.txt",
            }
        ],
    }


class ValidatePhysicalTestDataTests(unittest.TestCase):
    def test_checksum_validation_uses_manifest_without_known_disc_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "arbitrary.bin"
            payload.write_bytes(b"payload")
            digest = hashlib.sha256(b"payload").hexdigest()
            (root / "SHA256SUMS").write_text(
                f"{digest}  arbitrary.bin\n", encoding="utf-8"
            )
            self.assertEqual(
                validate.verify_checksums(root), {Path("arbitrary.bin"): digest}
            )

            payload.write_bytes(b"changed")
            with self.assertRaisesRegex(validate.ValidationError, "mismatch"):
                validate.verify_checksums(root)
            payload.write_bytes(b"payload")
            (root / "extra").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(validate.ValidationError, "unlisted"):
                validate.verify_checksums(root)

            (root / "extra").unlink()
            (root / "SHA256SUMS").write_text(
                f"{digest}  ../outside\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(validate.ValidationError, "unsafe"):
                validate.checksum_entries(root)

    def test_manifest_discovery_and_generic_command_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "synthetic"
            profile.mkdir()
            manifest = synthetic_manifest()
            (profile / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            loaded = validate.load_manifests(root)
            self.assertEqual(loaded[0][1], manifest)
            self.assertEqual(
                validate.selection_text([3, 4, 5]), "3-5"
            )
            self.assertEqual(validate.selection_text([7]), "7")
            with self.assertRaises(validate.ValidationError):
                validate.selection_text([1, 3])

            output = root / "output"
            output.mkdir()
            command = validate.extraction_command(
                Path("/tool"), profile, manifest["scenarios"][0], output
            )
            self.assertEqual(command[1], "1")
            self.assertIn("--no-accuraterip", command)
            self.assertNotIn("--include-data", command)
            self.assertNotIn(
                "--no-accuraterip",
                validate.extraction_command(
                    Path("/tool"),
                    profile,
                    manifest["scenarios"][0],
                    output,
                    accuraterip=True,
                ),
            )

            self.assertEqual(validate.load_manifests(root, ["absent"]), [])

            arguments = validate.parser().parse_args(
                ["--log-file", str(root / "validation.log")]
            )
            self.assertEqual(arguments.log_file, root / "validation.log")

            project_python = root / "project-python"
            project_python.touch()
            with mock.patch.object(validate, "PROJECT_PYTHON", project_python):
                self.assertEqual(
                    validate.launcher_command(validate.DEFAULT_LAUNCHER),
                    [str(project_python), str(validate.DEFAULT_LAUNCHER)],
                )

    def test_track_models_and_expected_output_names_are_manifest_driven(self):
        manifest = synthetic_manifest()
        track_zero = validate.selected_track(manifest, 0)
        self.assertEqual(track_zero["begin_lba"], 0)
        self.assertEqual(track_zero["length_sectors"], 10)
        self.assertEqual(validate.selected_track(manifest, 2)["kind"], "data")
        with self.assertRaises(validate.ValidationError):
            validate.selected_track(manifest, 9)

        scenario = dict(manifest["scenarios"][0], selected_tracks=[1, 2])
        output = Path("/tmp/output")
        expected = validate.expected_outputs(manifest, scenario, output)
        self.assertEqual([item[0].name for item in expected], ["track01.wav", "track02.iso"])

    def test_wav_and_iso_content_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav_path = root / "audio.wav"
            pcm = b"\x01\x02\x03\x04" * validate.PCM_FRAMES_PER_SECTOR
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(44100)
                output.writeframes(pcm)
            self.assertEqual(
                validate.wave_pcm_hash(wav_path, 1), hashlib.sha256(pcm).hexdigest()
            )
            with self.assertRaisesRegex(validate.ValidationError, "frame count"):
                validate.wave_pcm_hash(wav_path, 2)

            iso_path = root / "data.iso"
            blocks = 17
            image = bytearray(blocks * 2048)
            descriptor = memoryview(image)[16 * 2048:17 * 2048]
            descriptor[0:7] = b"\x01CD001\x01"
            descriptor[80:84] = blocks.to_bytes(4, "little")
            descriptor[84:88] = blocks.to_bytes(4, "big")
            descriptor[128:130] = (2048).to_bytes(2, "little")
            descriptor[130:132] = (2048).to_bytes(2, "big")
            descriptor[156] = 34
            descriptor[158:162] = (16).to_bytes(4, "little")
            descriptor[162:166] = (16).to_bytes(4, "big")
            descriptor[166:170] = (2048).to_bytes(4, "little")
            descriptor[170:174] = (2048).to_bytes(4, "big")
            iso_path.write_bytes(image)
            self.assertEqual(
                validate.iso_hash(iso_path, blocks), validate.sha256_file(iso_path)
            )
            with self.assertRaisesRegex(validate.ValidationError, "exceeds"):
                validate.iso_hash(iso_path, blocks - 1)
            descriptor[84:88] = (18).to_bytes(4, "big")
            iso_path.write_bytes(image)
            with self.assertRaisesRegex(validate.ValidationError, "disagree"):
                validate.iso_hash(iso_path, blocks)

            descriptor[84:88] = blocks.to_bytes(4, "big")
            descriptor[132:136] = (1).to_bytes(4, "little")
            descriptor[136:140] = (2).to_bytes(4, "big")
            iso_path.write_bytes(image)
            with self.assertRaisesRegex(validate.ValidationError, "path-table sizes"):
                validate.iso_hash(iso_path, blocks)

            descriptor[136:140] = (1).to_bytes(4, "big")
            descriptor[140:144] = blocks.to_bytes(4, "little")
            iso_path.write_bytes(image)
            with self.assertRaisesRegex(validate.ValidationError, "path table lies"):
                validate.iso_hash(iso_path, blocks)

            descriptor[132:140] = bytes(8)
            descriptor[140:144] = bytes(4)
            descriptor[156] = 0
            iso_path.write_bytes(image)
            with self.assertRaisesRegex(validate.ValidationError, "record is malformed"):
                validate.iso_hash(iso_path, blocks)

            descriptor[156] = 34
            descriptor[162:166] = (15).to_bytes(4, "big")
            iso_path.write_bytes(image)
            with self.assertRaisesRegex(validate.ValidationError, "directory fields"):
                validate.iso_hash(iso_path, blocks)

            descriptor[158:162] = blocks.to_bytes(4, "little")
            descriptor[162:166] = blocks.to_bytes(4, "big")
            iso_path.write_bytes(image)
            with self.assertRaisesRegex(validate.ValidationError, "directory lies"):
                validate.iso_hash(iso_path, blocks)

    def test_audio_parity_accounts_for_reported_write_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = [
                index.to_bytes(4, "little")
                for index in range(validate.PCM_FRAMES_PER_SECTOR + 2)
            ]

            def write(name, frames):
                path = root / name
                with wave.open(str(path), "wb") as output:
                    output.setnchannels(2)
                    output.setsampwidth(2)
                    output.setframerate(44100)
                    output.writeframes(b"".join(frames))
                return path

            first = write("first.wav", canonical[:-2])
            shifted = write("shifted.wav", canonical[2:])
            outputs = [
                validate.ParityOutput(first, "audio", 1, "unused", 0),
                validate.ParityOutput(shifted, "audio", 1, "unused", 2),
            ]
            validate.validate_track_parity("synthetic", 7, outputs)

            log = root / "validation.log"
            log.write_text("disc write offset: +2\n", encoding="utf-8")
            parsed = validate.parity_outputs(
                synthetic_manifest(),
                [(shifted, "audio", 2, 1)],
                {1: "digest"},
                log,
            )
            self.assertEqual(parsed[1].write_offset, 2)

            shifted.write_bytes(first.read_bytes())
            with self.assertRaisesRegex(validate.ValidationError, "differs"):
                validate.validate_track_parity("synthetic", 7, outputs)

    def test_data_parity_remains_an_exact_hash_comparison(self):
        matching = [
            validate.ParityOutput(Path("a"), "data", 1, "same"),
            validate.ParityOutput(Path("b"), "data", 1, "same"),
        ]
        validate.validate_track_parity("synthetic", 1, matching)
        with self.assertRaisesRegex(validate.ValidationError, "differs"):
            validate.validate_track_parity(
                "synthetic",
                1,
                [matching[0], validate.ParityOutput(Path("b"), "data", 1, "other")],
            )

        outputs = {1: matching[0], 2: matching[1]}
        self.assertIs(
            validate.parity_candidates(
                {"expected_media_errors": False}, outputs
            ),
            outputs,
        )
        self.assertEqual(
            validate.parity_candidates(
                {
                    "expected_media_errors": True,
                    "strict_accuraterip_track": 2,
                },
                outputs,
            ),
            {2: matching[1]},
        )
        self.assertEqual(
            validate.parity_candidates(
                {"expected_media_errors": True}, outputs
            ),
            {},
        )

        manifest = synthetic_manifest()
        scenario = manifest["scenarios"][0]
        self.assertEqual(validate.strict_known_clean_tracks(manifest, scenario), {1})
        self.assertEqual(
            validate.strict_known_clean_tracks(
                manifest, {**scenario, "fabricated_c2_track": 1}
            ),
            set(),
        )

    def test_parity_is_checked_incrementally_and_retains_one_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.iso"
            second = root / "second.iso"
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            baselines = {}
            retained = root / "retained"

            validate.record_track_parity(
                "synthetic",
                {1: validate.ParityOutput(first, "data", 1, "same")},
                baselines,
                retained,
            )
            self.assertFalse(first.exists())
            self.assertEqual(baselines[("synthetic", 1)].path.read_bytes(), b"same")

            validate.record_track_parity(
                "synthetic",
                {1: validate.ParityOutput(second, "data", 1, "same")},
                baselines,
                retained,
            )
            self.assertTrue(second.exists())
            self.assertEqual(len(baselines), 1)

            with self.assertRaisesRegex(validate.ValidationError, "differs"):
                validate.record_track_parity(
                    "synthetic",
                    {1: validate.ParityOutput(second, "data", 1, "different")},
                    baselines,
                )

    def test_command_status_and_offline_log_checks(self):
        success = SimpleNamespace(returncode=0, stdout="ok")
        with mock.patch.object(validate.subprocess, "run", return_value=success):
            self.assertIs(
                validate.run_command(["tool"], Path("/tmp"), True), success
            )
        with tempfile.TemporaryDirectory() as directory:
            detail = Path(directory) / "detail.log"
            detail.write_text("verbose detail\n", encoding="utf-8")
            stream = io.StringIO()
            console = io.StringIO()
            validation_log = validate.ValidationLog(stream)
            with redirect_stdout(console):
                with validate.validation_test(validation_log, "[synthetic] example"):
                    with mock.patch.object(
                        validate.subprocess, "run", return_value=success
                    ):
                        validate.run_command(
                            ["tool", f"--log-file={detail}"],
                            Path(directory),
                            True,
                            validation_log,
                        )
            rendered = stream.getvalue()
            self.assertIn("=" * 80, rendered)
            self.assertIn("TEST: [synthetic] example", rendered)
            self.assertIn(
                "TEST RESULT: PASS — [synthetic] example", rendered
            )
            self.assertIn(f"Working directory: {directory}", rendered)
            self.assertIn("Exit status: 0", rendered)
            self.assertIn("Captured output", rendered)
            self.assertIn("verbose detail", rendered)
            with redirect_stdout(console):
                with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                    with validate.validation_test(
                        validation_log, "[synthetic] failure"
                    ):
                        raise RuntimeError("synthetic failure")
            self.assertIn(
                "TEST RESULT: FAIL — [synthetic] failure", stream.getvalue()
            )
            self.assertIn("[synthetic] example ... PASS", console.getvalue())
            self.assertIn("[synthetic] failure ... FAIL", console.getvalue())
            with redirect_stdout(console):
                validate.skipped_test(
                    validation_log, "[synthetic] unavailable", "no data available"
                )
            self.assertIn(
                "TEST RESULT: SKIP — [synthetic] unavailable", stream.getvalue()
            )
            self.assertIn("Skip reason: no data available", stream.getvalue())
            self.assertIn(
                "[synthetic] unavailable ... SKIP (no data available)",
                console.getvalue(),
            )
            failure = SimpleNamespace(returncode=2, stdout="failed")
            with mock.patch.object(validate.subprocess, "run", return_value=failure):
                with self.assertRaisesRegex(
                    validate.ValidationError, "(?s)Extraction log tail.*verbose detail"
                ):
                    validate.run_command(
                        ["tool", f"--log-file={detail}"], Path(directory), True
                    )
        failure = SimpleNamespace(returncode=2, stdout="failed")
        with mock.patch.object(validate.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(validate.ValidationError, "expected success"):
                validate.run_command(["tool"], Path("/tmp"), True)

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "validation.log"
            log.write_text("+ redumper split --force-split\n", encoding="utf-8")
            validate.assert_offline_log(log)
            log.write_text(
                "+ redumper dump --drive=/dev/sg4\n+ redumper split\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(validate.ValidationError, "acquisition"):
                validate.assert_offline_log(log)

    def test_accuraterip_requirements_are_state_driven_and_fail_closed(self):
        manifest = synthetic_manifest()
        scenario = manifest["scenarios"][0]
        self.assertEqual(
            validate.accuraterip_tracks(manifest, scenario),
            (1,),
        )
        self.assertEqual(
            validate.accuraterip_tracks(
                manifest,
                dict(scenario, selected_tracks=[0, 2]),
            ),
            (),
        )
        data_first = synthetic_manifest()
        data_first["layout"]["tracks"] = [
            data_first["layout"]["tracks"][1],
            data_first["layout"]["tracks"][0],
        ]
        self.assertEqual(
            validate.accuraterip_tracks(data_first, scenario),
            (),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "dump"
            log = root / "validation.log"
            log.write_text("disc write offset: 0\n", encoding="utf-8")
            scenario = dict(scenario, existing_dump="dump")
            with mock.patch.object(
                validate,
                "inspect_track_media_errors",
                return_value={1: {"SCSI": 0, "C2": 0}},
            ) as inspect:
                self.assertEqual(
                    validate.expected_accuraterip_results(
                        root, manifest, scenario, log
                    ),
                    {1: "verified"},
                )
            self.assertEqual(inspect.call_args.args[0], prefix.with_suffix(".state"))
            with mock.patch.object(
                validate,
                "inspect_track_media_errors",
                return_value={1: {"SCSI": 0, "C2": 3}},
            ):
                self.assertEqual(
                    validate.expected_accuraterip_results(
                        root, manifest, scenario, log
                    ),
                    {1: "no match"},
                )

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "validation.log"
            log.write_text(
                "Track 01: verified (ARv2 12345678, confidence 4)\n",
                encoding="utf-8",
            )
            validate.assert_accuraterip(log, {1: "verified"})
            log.write_text(
                "Track 01: no match (ARv1 1, ARv2 2)\n",
                encoding="utf-8",
            )
            validate.assert_accuraterip(log, {1: "no match"})
            with self.assertRaisesRegex(validate.ValidationError, "result mismatch"):
                validate.assert_accuraterip(log, {1: "verified"})
            with self.assertRaisesRegex(validate.ValidationError, "results differ"):
                validate.assert_accuraterip(log, {1: "verified", 2: "verified"})

            validate.assert_accuraterip_track(log, 1, "no match")
            with self.assertRaisesRegex(validate.ValidationError, "expected verified"):
                validate.assert_accuraterip_track(log, 1, "verified")
            with self.assertRaisesRegex(validate.ValidationError, "got missing"):
                validate.assert_accuraterip_track(log, 2, "verified")

    def test_required_nonzero_write_offset_is_manifest_driven(self):
        manifest = synthetic_manifest()
        scenario = {
            **manifest["scenarios"][0],
            "strict_accuraterip_track": 1,
            "required_write_offset_sign": -1,
        }
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "validation.log"
            log.write_text("disc write offset: -12\n", encoding="utf-8")
            validate.assert_required_write_offset(manifest, scenario, log)

            log.write_text("disc write offset: +12\n", encoding="utf-8")
            with self.assertRaisesRegex(validate.ValidationError, "negative"):
                validate.assert_required_write_offset(manifest, scenario, log)

            scenario["required_write_offset_sign"] = 0
            log.write_text("unparseable", encoding="utf-8")
            validate.assert_required_write_offset(manifest, scenario, log)

    def test_fabricates_c2_only_in_a_disposable_state_copy(self):
        manifest = synthetic_manifest()
        scenario = {
            **manifest["scenarios"][0],
            "fabricated_c2_track": 1,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            source_dir = profile / "case"
            source_dir.mkdir(parents=True)
            source_prefix = source_dir / "disc"
            scenario["existing_dump"] = "case/disc"
            scenario["cdparanoia_toc_file"] = "toc.txt"
            (profile / "toc.txt").write_text("toc", encoding="utf-8")
            track = validate.selected_track(manifest, 1)
            lba = track["begin_lba"] + track["length_sectors"] // 2
            file_sample = (
                (lba - validate.REDUMPER_LBA_START)
                * validate.SAMPLES_PER_SECTOR
                - 12
            )
            state = source_prefix.with_suffix(".state")
            with state.open("wb") as stream:
                stream.seek(file_sample)
                stream.write(b"\x02")
            (source_prefix.with_suffix(".toc")).write_bytes(b"toc")
            log = root / "split.log"
            log.write_text("disc write offset: -12\n", encoding="utf-8")
            destination = root / "fabricated"

            derived_root, derived = validate.fabricate_c2_dump(
                profile, manifest, scenario, destination, log
            )

            derived_state = (
                derived_root / derived["existing_dump"]
            ).with_suffix(".state")
            with derived_state.open("rb") as stream:
                stream.seek(file_sample)
                self.assertEqual(stream.read(1), b"\x01")
            with state.open("rb") as stream:
                stream.seek(file_sample)
                self.assertEqual(stream.read(1), b"\x02")
            record = json.loads(
                (destination / "fabricated-c2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["track"], 1)
            self.assertIn("PCM is unchanged", record["kind"])

    def test_capture_offset_requirements_fail_closed(self):
        data_last_scenarios = [
            {
                "selected_tracks": [1],
                "include_data": False,
            },
            {
                "selected_tracks": [1, 2, 3],
                "include_data": True,
                "strict_accuraterip_track": 1,
                "fabricated_c2_track": 2,
                "omitted_alignment_track": 2,
                "required_write_offset_sign": -1,
            },
        ]
        validate.validate_capture_write_offsets(
            {
                "profile": "regular-audio",
                "write_offset_probe": {"offsets": [[0, 0]]},
            }
        )
        with self.assertRaisesRegex(validate.ValidationError, "requires zero"):
            validate.validate_capture_write_offsets(
                {
                    "profile": "regular-audio",
                    "write_offset_probe": {"offsets": [[0, -12]]},
                }
            )
        validate.validate_capture_write_offsets(
            {
                "profile": "data-last",
                "scenarios": data_last_scenarios,
                "write_offset_probe": {"offsets": [[0, -12]]},
            }
        )
        with self.assertRaisesRegex(validate.ValidationError, "negative nonzero"):
            validate.validate_capture_write_offsets(
                {
                    "profile": "data-last",
                    "scenarios": data_last_scenarios,
                    "write_offset_probe": {"offsets": [[0, 0]]},
                }
            )
        with self.assertRaisesRegex(validate.ValidationError, "lacks a valid"):
            validate.validate_capture_write_offsets({"profile": "data-last"})
        validate.validate_capture_write_offsets(
            {"profile": "regular-audio"}, [0]
        )
        validate.validate_capture_write_offsets(
            {"profile": "data-last", "scenarios": data_last_scenarios}, [-12]
        )
        with self.assertRaisesRegex(validate.ValidationError, "lacks the merged"):
            validate.validate_capture_write_offsets(
                {"profile": "data-last", "scenarios": []}, [-12]
            )
        validate.validate_capture_write_offsets({"profile": "data-only"})

    def test_layout_probe_prefers_manifest_probe_then_complete_scenario(self):
        manifest = synthetic_manifest()
        first = manifest["scenarios"][0]
        complete = {**first, "name": "complete", "selected_tracks": [1]}
        manifest["scenarios"] = [complete]
        self.assertIs(validate.layout_probe_scenario(manifest), complete)

        manifest["scenarios"] = [first, complete]
        manifest["write_offset_probe"] = {"scenario": first["name"]}
        self.assertIs(validate.layout_probe_scenario(manifest), first)


if __name__ == "__main__":
    unittest.main()
