import io
import tempfile
import unittest
import wave
from pathlib import Path

from redumper_cdda.adapters.wav import SECTOR_SIZE, WaveOutputWriter
from redumper_cdda.domain.errors import AudioOutputError
from redumper_cdda.domain.outputs import AudioSegment, OutputKind, OutputPlan, ResolvedOutput


class WaveOutputWriterTests(unittest.TestCase):
    def resolved(self, source, sectors=1, expected=1):
        plan = OutputPlan(None, OutputKind.AUDIO, (), expected, Path("out.wav"))
        segment = AudioSegment(source, 1, 0, sectors, sectors)
        return ResolvedOutput(plan, (segment,))

    def test_writes_pcm_directly_and_reports_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "audio.bin"
            payload = bytes(range(256)) * (SECTOR_SIZE // 256) + bytes(
                range(SECTOR_SIZE % 256)
            )
            source.write_bytes(payload)
            messages = []
            destination = root / "audio.wav"
            WaveOutputWriter(messages.append).write(
                self.resolved(source), destination, verbose=True
            )
            with wave.open(str(destination), "rb") as wav:
                self.assertEqual(wav.readframes(SECTOR_SIZE), payload)
            self.assertIn("WAV conversion", messages[0])
            self.assertIn("Total sectors", messages[-1])

    def test_rejects_wrong_total_and_short_bin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "audio.bin"
            source.write_bytes(b"short")
            writer = WaveOutputWriter()
            with self.assertRaisesRegex(AudioOutputError, "sector count"):
                writer.write(self.resolved(source, expected=2), root / "one.wav")
            with self.assertRaisesRegex(AudioOutputError, "Unexpected end"):
                writer.write(self.resolved(source), root / "two.wav")


if __name__ == "__main__":
    unittest.main()
