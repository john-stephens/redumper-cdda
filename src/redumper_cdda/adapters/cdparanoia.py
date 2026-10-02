"""cdparanoia audio-layout adapter."""

from ..domain.disc import AudioLayout, Track, TrackKind
from ..domain.errors import LayoutError, TocParseError
from ..domain.events import LifecycleEvent
from ..layout import parse_cdparanoia_toc


class CdparanoiaTocReader:
    def __init__(self, runner, reporter):
        self._runner = runner
        self._reporter = reporter

    @staticmethod
    def command(device):
        return ["cdparanoia", "-Q", "-d", device]

    def read(self, device):
        self._reporter.publish(
            LifecycleEvent("layout_read", "Reading audio track layout with cdparanoia...")
        )

        result = self._runner.capture(
            self.command(device),
            text=True,
            merge_stderr=False,
        )
        if result.returncode != 0:
            details = "\n".join(
                part.strip()
                for part in (result.output or "", result.stderr or "")
                if part.strip()
            )
            raise LayoutError(
                "cdparanoia -Q failed"
                + ("\n\n" + details if details else "")
            )

        output = (result.output or "") + "\n" + (result.stderr or "")
        try:
            return AudioLayout(
                tuple(
                    Track(
                        item["number"], TrackKind.AUDIO, 0,
                        item["begin"], item["end"],
                        item["length_msf"], item["begin_msf"],
                    )
                    for item in parse_cdparanoia_toc(output)
                )
            )
        except RuntimeError as exc:
            raise TocParseError(str(exc)) from exc
