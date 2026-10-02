"""MMC READ TOC format-0 layout adapter."""

from ..domain.disc import DiscLayout, Track, TrackKind
from ..domain.errors import LayoutError, TocParseError
from ..domain.events import LifecycleEvent
from ..layout import parse_mmc_toc


MMC_TOC_ALLOCATION_LENGTH = 804


class MmcTocReader:
    def __init__(self, runner, reporter):
        self._runner = runner
        self._reporter = reporter

    @staticmethod
    def command(device):
        allocation_msb = (MMC_TOC_ALLOCATION_LENGTH >> 8) & 0xFF
        allocation_lsb = MMC_TOC_ALLOCATION_LENGTH & 0xFF
        return [
            "sg_raw",
            "--readonly",
            "--binary",
            f"--request={MMC_TOC_ALLOCATION_LENGTH}",
            device,
            "43",
            "00",
            "00",
            "00",
            "00",
            "00",
            "00",
            f"{allocation_msb:02x}",
            f"{allocation_lsb:02x}",
            "00",
        ]

    def read(self, device):
        self._reporter.publish(
            LifecycleEvent(
                "layout_read", "Reading complete track layout with MMC READ TOC..."
            )
        )

        result = self._runner.capture(
            self.command(device),
            text=False,
            merge_stderr=False,
        )
        if result.returncode != 0:
            details = (result.stderr or b"").decode(
                "utf-8",
                errors="replace",
            ).strip()
            raise LayoutError(
                "MMC READ TOC failed"
                + ("\n\n" + details if details else "")
            )

        try:
            tracks = tuple(_track(item) for item in parse_mmc_toc(result.output))
            return DiscLayout(tracks, tracks[-1].end_lba)
        except RuntimeError as exc:
            raise TocParseError(str(exc)) from exc


def _track(item):
    return Track(
        item["number"],
        TrackKind(item["kind"]),
        item["control"],
        item["begin"],
        item["end"],
        item["length_msf"],
        item["begin_msf"],
    )
