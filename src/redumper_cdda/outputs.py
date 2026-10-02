"""Temporary WAV assembly used by the ARver integration."""

import wave


SECTOR_SIZE = 2352
SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2


def segments_to_wav(
    segments,
    wav_path,
    expected_sectors,
    verbose=False,
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

    block_sectors = 1024

    with wave.open(
        str(wav_path),
        "wb",
    ) as dst:

        dst.setnchannels(CHANNELS)
        dst.setsampwidth(SAMPLE_WIDTH)
        dst.setframerate(SAMPLE_RATE)

        for segment in segments:

            with segment["path"].open(
                "rb"
            ) as src:

                src.seek(
                    segment["start_sector"]
                    * SECTOR_SIZE
                )

                remaining = (
                    segment["sectors"]
                )

                while remaining > 0:

                    count = min(
                        block_sectors,
                        remaining,
                    )

                    expected_bytes = (
                        count
                        * SECTOR_SIZE
                    )

                    data = src.read(
                        expected_bytes
                    )

                    if (
                        len(data)
                        != expected_bytes
                    ):
                        raise RuntimeError(
                            "Unexpected end of BIN."
                        )

                    # No endian swap.
                    dst.writeframesraw(
                        data
                    )

                    remaining -= count
