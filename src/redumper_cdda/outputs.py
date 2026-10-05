"""Temporary WAV assembly used by the ARver integration."""

import wave


SECTOR_SIZE = 2352
SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2
BYTES_PER_FRAME = CHANNELS * SAMPLE_WIDTH
FRAMES_PER_SECTOR = SECTOR_SIZE // BYTES_PER_FRAME


def _write_segment_window(destination, segments, start_frame, frames):
    remaining = frames
    skip = start_frame
    block_frames = FRAMES_PER_SECTOR * 1024

    for segment in segments:
        segment_frames = segment["sectors"] * FRAMES_PER_SECTOR
        if skip >= segment_frames:
            skip -= segment_frames
            continue

        take = min(segment_frames - skip, remaining)
        with segment["path"].open("rb") as source:
            source_frame = (
                segment["start_sector"] * FRAMES_PER_SECTOR + skip
            )
            source.seek(source_frame * BYTES_PER_FRAME)
            pending = take
            while pending:
                count = min(block_frames, pending)
                data = source.read(count * BYTES_PER_FRAME)
                if len(data) != count * BYTES_PER_FRAME:
                    raise RuntimeError("Unexpected end of BIN.")
                destination.writeframesraw(data)
                pending -= count
        remaining -= take
        skip = 0
        if remaining == 0:
            return

    raise RuntimeError(
        "Insufficient adjacent split AUDIO data for AccurateRip alignment."
    )


def segments_to_wav(
    segments,
    wav_path,
    expected_sectors,
    verbose=False,
    write_offset=0,
    preceding_segments=(),
    following_segments=(),
):
    total_sectors = sum(
        segment["sectors"]
        for segment in segments
    )

    if total_sectors != expected_sectors:
        raise RuntimeError(
            "Selected CDDA segments do not match "
            "the requested sector count."
        )

    if verbose:
        print()
        print("WAV conversion")
        print("--------------")

        for index, segment in enumerate(
            segments,
            start=1,
        ):
            end_sector = (
                segment["start_sector"]
                + segment["sectors"]
            )

            print(f"Segment {index}:")
            print(
                f"  BIN:               "
                f"{segment['path']}"
            )
            print(
                f"  Redumper track:    "
                f"{segment['track']:02d}"
            )
            print(
                f"  Start sector:      "
                f"{segment['start_sector']:,}"
            )
            print(
                f"  End sector:        "
                f"{end_sector:,} [exclusive]"
            )
            print(
                f"  Sectors used:      "
                f"{segment['sectors']:,}"
            )

        print()
        print(
            f"Total sectors:       "
            f"{total_sectors:,}"
        )
        print(
            f"Temporary WAV:       "
            f"{wav_path}"
        )

    wav_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    preceding_frames = sum(
        segment["sectors"] * FRAMES_PER_SECTOR
        for segment in preceding_segments
    )
    start_frame = preceding_frames - write_offset
    output_frames = expected_sectors * FRAMES_PER_SECTOR
    all_segments = (
        tuple(preceding_segments) + tuple(segments) + tuple(following_segments)
    )
    available_frames = sum(
        segment["sectors"] * FRAMES_PER_SECTOR
        for segment in all_segments
    )
    if start_frame < 0 or start_frame + output_frames > available_frames:
        raise RuntimeError(
            "Insufficient adjacent split AUDIO data for AccurateRip alignment."
        )

    with wave.open(
        str(wav_path),
        "wb",
    ) as dst:

        dst.setnchannels(CHANNELS)
        dst.setsampwidth(SAMPLE_WIDTH)
        dst.setframerate(SAMPLE_RATE)

        _write_segment_window(
            dst, all_segments, start_frame, output_frames
        )
