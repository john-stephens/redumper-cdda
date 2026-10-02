"""AccurateRip identification, checksum matching, and reporting."""

import io
from contextlib import redirect_stdout

from .outputs import segments_to_wav


ACCURATERIP_LEAD_IN_SECTORS = 150


def load_accuraterip_library():
    try:
        from arver.audio.checksums import get_checksums
        from arver.disc.database import AccurateRipFetcher
        from arver.disc.fingerprint import (
            accuraterip_ids,
            freedb_id,
        )
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "AccurateRip verification requires the ARver Python "
            "package. Install it with: python3 -m pip install arver"
        ) from exc

    return {
        "get_checksums": get_checksums,
        "fetcher": AccurateRipFetcher,
        "accuraterip_ids": accuraterip_ids,
        "freedb_id": freedb_id,
    }


def accuraterip_layout_type(disc_tracks):
    kinds = [
        track.get("kind", "audio")
        for track in disc_tracks
    ]

    if not kinds or "audio" not in kinds:
        raise RuntimeError(
            "AccurateRip verification requires at least one audio track."
        )

    if all(kind == "audio" for kind in kinds):
        return "audio"

    if (
        kinds[0] == "data"
        and all(kind == "audio" for kind in kinds[1:])
    ):
        return "mixed-mode"

    if (
        kinds[-1] == "data"
        and all(kind == "audio" for kind in kinds[:-1])
    ):
        return "enhanced"

    raise RuntimeError(
        "AccurateRip verification does not support this audio/data "
        "track arrangement."
    )


def build_accuraterip_disc_id(
    disc_tracks,
    library=None,
):
    if library is None:
        library = load_accuraterip_library()

    accuraterip_layout_type(
        disc_tracks
    )
    audio_tracks = [
        track
        for track in disc_tracks
        if track.get("kind", "audio") == "audio"
    ]
    all_offsets = [
        track["begin"] + ACCURATERIP_LEAD_IN_SECTORS
        for track in disc_tracks
    ]
    audio_offsets = [
        track["begin"] + ACCURATERIP_LEAD_IN_SECTORS
        for track in audio_tracks
    ]
    lead_out = (
        disc_tracks[-1]["end"]
        + ACCURATERIP_LEAD_IN_SECTORS
    )
    ar_id1, ar_id2 = library[
        "accuraterip_ids"
    ](
        audio_offsets,
        lead_out,
    )
    cddb_id = library["freedb_id"](
        all_offsets,
        lead_out,
    )

    return (
        f"{len(audio_tracks):03d}-"
        f"{ar_id1}-{ar_id2}-{cddb_id}"
    )


def match_accuraterip_checksums(
    arv1,
    arv2,
    candidates,
):
    for version, checksum in (
        ("ARv2", arv2),
        ("ARv1", arv1),
    ):
        if checksum in candidates:
            match = candidates[checksum]
            return {
                "status": "verified",
                "version": version,
                "checksum": checksum,
                "confidence": match["confidence"],
                "response": match["response"],
            }

    return {
        "status": (
            "no-match"
            if candidates
            else "not-present"
        ),
        "arv1": arv1,
        "arv2": arv2,
    }


def verify_with_accuraterip(
    disc_tracks,
    verification_tracks,
    workdir,
    library=None,
):
    if library is None:
        library = load_accuraterip_library()

    layout_type = accuraterip_layout_type(
        disc_tracks
    )
    audio_tracks = [
        track
        for track in disc_tracks
        if track.get("kind", "audio") == "audio"
    ]
    audio_indexes = {
        track["number"]: index
        for index, track in enumerate(
            audio_tracks,
            start=1,
        )
    }
    requested = [
        item
        for item in verification_tracks
        if item["track"]["number"] != 0
    ]

    if not requested:
        raise RuntimeError(
            "The selection contains no AccurateRip-verifiable "
            "audio tracks; Track 0 and data tracks are not tracked."
        )

    disc_id = build_accuraterip_disc_id(
        disc_tracks,
        library=library,
    )
    fetch_output = io.StringIO()

    try:
        with redirect_stdout(fetch_output):
            database_disc = library[
                "fetcher"
            ].from_id(disc_id).fetch()
    except Exception as exc:
        raise RuntimeError(
            f"ARver database lookup failed: {exc}"
        ) from exc

    if database_disc is None:
        detail = fetch_output.getvalue().strip()
        raise RuntimeError(
            detail
            or "The disc was not found in the AccurateRip database."
        )

    try:
        database_tracks = database_disc.make_dict()
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"ARver returned unusable database data: {exc}"
        ) from exc
    results = []

    for item in requested:
        track = item["track"]
        audio_index = audio_indexes[
            track["number"]
        ]
        wav_path = (
            workdir
            / f"accuraterip-track{track['number']:02d}.wav"
        )

        segments_to_wav(
            item["segments"],
            wav_path,
            track["length"],
        )

        try:
            checksums = library[
                "get_checksums"
            ](
                str(wav_path),
                audio_index,
                len(audio_tracks),
            )
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Could not checksum Track {track['number']:02d}: {exc}"
            ) from exc

        database_index = (
            track["number"]
            if layout_type == "mixed-mode"
            else audio_index
        )
        candidates = database_tracks.get(
            database_index,
            {},
        )
        result = match_accuraterip_checksums(
            checksums.arv1,
            checksums.arv2,
            candidates,
        )
        result["track"] = track["number"]
        results.append(result)

    return {
        "disc_id": disc_id,
        "results": results,
    }


def print_accuraterip_report(report, verbose=False):
    print()
    print("AccurateRip verification")
    print("========================")
    print(f"Disc ID: {report['disc_id']}")
    print()

    verified = 0

    for result in report["results"]:
        track_label = f"Track {result['track']:02d}"

        if result["status"] == "verified":
            verified += 1
            response = (
                f", response {result['response']}"
                if verbose
                else ""
            )
            print(
                f"{track_label}: verified "
                f"({result['version']} "
                f"{result['checksum']:08x}, "
                f"confidence {result['confidence']}"
                f"{response})"
            )
        elif result["status"] == "not-present":
            print(
                f"{track_label}: not present in the database "
                f"(ARv1 {result['arv1']:08x}, "
                f"ARv2 {result['arv2']:08x})"
            )
        else:
            print(
                f"{track_label}: no match "
                f"(ARv1 {result['arv1']:08x}, "
                f"ARv2 {result['arv2']:08x})"
            )

    total = len(report["results"])
    print()
    print(
        f"Verified: {verified}/{total} selected audio "
        f"track{'' if total == 1 else 's'}"
    )
