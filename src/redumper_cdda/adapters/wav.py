"""Direct PCM-to-WAV output adapter."""

import wave

from ..domain.errors import AudioOutputError
from ..layout import sectors_to_msf


SECTOR_SIZE = 2352
SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2


class WaveOutputWriter:
    def __init__(self, output=print):
        self._output = output

    def write(self, resolved, temporary_path, verbose=False):
        segments = resolved.audio_segments
        total_sectors = sum(segment.sectors for segment in segments)
        if total_sectors != resolved.plan.expected_sectors:
            raise AudioOutputError(
                "Selected CDDA segments do not match the requested sector count."
            )
        if verbose:
            self._print_details(segments, total_sectors, temporary_path)
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(temporary_path), "wb") as destination:
            destination.setnchannels(CHANNELS)
            destination.setsampwidth(SAMPLE_WIDTH)
            destination.setframerate(SAMPLE_RATE)
            for segment in segments:
                self._write_segment(destination, segment)

    @staticmethod
    def _write_segment(destination, segment):
        with segment.path.open("rb") as source:
            source.seek(segment.start_sector * SECTOR_SIZE)
            remaining = segment.sectors
            while remaining > 0:
                count = min(1024, remaining)
                expected_bytes = count * SECTOR_SIZE
                data = source.read(expected_bytes)
                if len(data) != expected_bytes:
                    raise AudioOutputError("Unexpected end of BIN.")
                destination.writeframesraw(data)
                remaining -= count

    def _print_details(self, segments, total_sectors, temporary_path):
        self._output("\nWAV conversion\n--------------")
        for index, segment in enumerate(segments, start=1):
            self._output(
                f"Segment {index}:\n"
                f"  BIN:               {segment.path}\n"
                f"  Redumper track:    {segment.track_number:02d}\n"
                f"  Start sector:      {segment.start_sector:,}\n"
                f"  End sector:        "
                f"{segment.start_sector + segment.sectors:,} [exclusive]\n"
                f"  Sectors used:      {segment.sectors:,}"
            )
        self._output(
            f"\nTotal sectors:       {total_sectors:,}\n"
            f"Duration:            {sectors_to_msf(total_sectors)}\n"
            f"Temporary WAV:       {temporary_path}"
        )
