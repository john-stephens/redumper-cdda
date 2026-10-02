"""Reconciled complete-disc layout provider."""

from ..domain.disc import DiscLayout, Track, TrackKind
from ..domain.errors import LayoutMismatchError


class ReconciledLayoutProvider:
    def __init__(self, mmc_reader, audio_reader):
        self._mmc_reader = mmc_reader
        self._audio_reader = audio_reader

    def read(self, device):
        mmc_layout = self._mmc_reader.read(device)
        mmc_audio = mmc_layout.audio_tracks()
        if not mmc_audio:
            return mmc_layout

        audio_layout = self._audio_reader.read(device)
        audio_by_number = {track.number: track for track in audio_layout.tracks}
        mmc_numbers = {track.number for track in mmc_audio}
        audio_numbers = set(audio_by_number)
        if mmc_numbers != audio_numbers:
            raise LayoutMismatchError(
                "MMC and cdparanoia disagree about which tracks are audio "
                f"(MMC: {sorted(mmc_numbers)}, "
                f"cdparanoia: {sorted(audio_numbers)})."
            )

        reconciled = []
        for mmc_track in mmc_layout.tracks:
            if mmc_track.kind is TrackKind.DATA:
                reconciled.append(mmc_track)
                continue

            audio_track = audio_by_number[mmc_track.number]
            differences = []
            for field, mmc_value, audio_value in (
                ("begin", mmc_track.begin_lba, audio_track.begin_lba),
                ("end", mmc_track.end_lba, audio_track.end_lba),
                (
                    "length",
                    mmc_track.length_sectors,
                    audio_track.length_sectors,
                ),
            ):
                if mmc_value != audio_value:
                    differences.append(
                        f"{field}: MMC={mmc_value}, cdparanoia={audio_value}"
                    )
            if differences:
                raise LayoutMismatchError(
                    "MMC and cdparanoia boundaries disagree for audio "
                    f"Track {mmc_track.number} ({', '.join(differences)})."
                )

            reconciled.append(
                Track(
                    number=mmc_track.number,
                    kind=TrackKind.AUDIO,
                    control=mmc_track.control,
                    begin_lba=audio_track.begin_lba,
                    end_lba=audio_track.end_lba,
                    length_msf=audio_track.length_msf,
                    begin_msf=audio_track.begin_msf,
                )
            )

        return DiscLayout(tuple(reconciled), mmc_layout.lead_out_lba)
