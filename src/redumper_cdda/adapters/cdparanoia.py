"""cdparanoia audio-layout adapter."""

from pathlib import Path

from ..domain.disc import AudioLayout, Track, TrackKind
from ..domain.errors import LayoutError, TocParseError
from ..domain.events import LifecycleEvent
from ..layout import parse_cdparanoia_toc


class CdparanoiaTocReader:
    def __init__(self, runner, reporter, toc_path=None):
        self._runner = runner
        self._reporter = reporter
        self._toc_path = Path(toc_path) if toc_path is not None else None

    @staticmethod
    def command(device):
        return ["cdparanoia", "-Q", "-d", device]

    def read(self, device):
        if self._toc_path is not None:
            return self._read_file()
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
        return self._parse(output)

    def _read_file(self):
        self._reporter.publish(
            LifecycleEvent(
                "layout_read",
                "Reading audio track layout from cdparanoia TOC file...",
            )
        )
        try:
            output = self._toc_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LayoutError(
                f"Could not read cdparanoia TOC file: {exc}"
            ) from exc
        return self._parse(output)

    @staticmethod
    def _parse(output):
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
