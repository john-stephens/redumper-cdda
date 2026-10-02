"""Output planning and transactional WAV/ISO creation."""

import wave
from pathlib import Path

from .iso9660 import data_track_to_iso
from .layout import sectors_to_msf


SECTOR_SIZE = 2352
SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2


def build_output_jobs(
    selected_tracks,
    single_file,
    output=None,
    output_directory=None,
    prefix="track",
):
    directory = (
        Path.cwd()
        if output_directory is None
        else Path(output_directory)
    )

    if not single_file:
        return [
            {
                "track": track,
                "kind": track.get("kind", "audio"),
                "component_tracks": (
                    [track]
                    if track.get("kind", "audio") == "audio"
                    else []
                ),
                "expected_sectors": track["length"],
                "output_path": (
                    directory
                    / (
                        f"{prefix}{track['number']:02d}.iso"
                        if track.get("kind", "audio") == "data"
                        else f"{prefix}{track['number']:02d}.wav"
                    )
                ).resolve(),
            }
            for track in selected_tracks
        ]

    first_track = selected_tracks[0]

    if (
        len(selected_tracks) > 1
        and any(
            track.get("kind", "audio") == "data"
            for track in selected_tracks
        )
    ):
        raise RuntimeError(
            "Data tracks can only be combined with other "
            "tracks as separate files."
        )

    if first_track.get("kind", "audio") == "data":
        output_path = (
            Path(output).expanduser().resolve()
            if output is not None
            else (
                directory
                / f"{prefix}{first_track['number']:02d}.iso"
            ).resolve()
        )
        return [
            {
                "track": first_track,
                "kind": "data",
                "component_tracks": [],
                "expected_sectors": first_track["length"],
                "output_path": output_path,
            }
        ]

    output_sectors = sum(
        track["length"]
        for track in selected_tracks
    )
    wav_path = (
        Path(output).expanduser().resolve()
        if output is not None
        else (
            directory
            / (
                f"{prefix}{first_track['number']:02d}.wav"
                if len(selected_tracks) == 1
                else f"{prefix}.wav"
            )
        ).resolve()
    )

    return [
        {
            "track": None,
            "kind": "audio",
            "component_tracks": selected_tracks,
            "expected_sectors": output_sectors,
            "output_path": wav_path,
        }
    ]


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
            f"Duration:            "
            f"{sectors_to_msf(total_sectors)}"
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


def remove_output_job_files(output_jobs):
    for job in output_jobs:
        for path in (
            job["temporary_path"],
            job["output_path"],
        ):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass


def create_output_files(output_jobs, verbose=False):
    try:
        for job in output_jobs:
            if job["kind"] == "data":
                data_track_to_iso(
                    job["data_track"],
                    job["temporary_path"],
                    verbose=verbose,
                )
            else:
                segments_to_wav(
                    job["segments"],
                    job["temporary_path"],
                    job["expected_sectors"],
                    verbose=verbose,
                )

        for job in output_jobs:
            job["temporary_path"].replace(
                job["output_path"]
            )

    except BaseException:
        remove_output_job_files(
            output_jobs
        )
        raise
