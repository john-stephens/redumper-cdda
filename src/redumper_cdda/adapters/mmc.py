"""MMC READ TOC format-0 layout adapter."""

from pathlib import Path

from ..domain.disc import DiscLayout, Track, TrackKind
from ..domain.errors import LayoutError, TocParseError
from ..domain.events import LifecycleEvent
from ..layout import apply_session_boundaries, parse_mmc_full_toc, parse_mmc_toc


MMC_TOC_ALLOCATION_LENGTH = 804
MMC_FULL_TOC_ALLOCATION_LENGTH = 4096


class MmcTocReader:
    def __init__(self, runner, reporter, toc_path=None, full_toc_path=None):
        self._runner = runner
        self._reporter = reporter
        self._toc_path = Path(toc_path) if toc_path is not None else None
        self._full_toc_path = (
            Path(full_toc_path) if full_toc_path is not None else None
        )

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

    @staticmethod
    def full_toc_command(device):
        allocation_msb = (MMC_FULL_TOC_ALLOCATION_LENGTH >> 8) & 0xFF
        allocation_lsb = MMC_FULL_TOC_ALLOCATION_LENGTH & 0xFF
        return [
            "sg_raw",
            "--readonly",
            "--binary",
            f"--request={MMC_FULL_TOC_ALLOCATION_LENGTH}",
            device,
            "43",
            "02",
            "02",
            "00",
            "00",
            "00",
            "01",
            f"{allocation_msb:02x}",
            f"{allocation_lsb:02x}",
            "00",
        ]

    def read(self, device):
        if self._toc_path is not None:
            return self._read_files()
        self._reporter.publish(
            LifecycleEvent(
                "layout_read", "Reading complete track layout with MMC READ TOC..."
            )
        )

        result = self._capture(self.command(device), "MMC READ TOC")
        full_toc_result = self._capture(
            self.full_toc_command(device), "MMC READ TOC format 2"
        )

        return self._parse(result.output, full_toc_result.output)

    def _read_files(self):
        self._reporter.publish(
            LifecycleEvent(
                "layout_read",
                "Reading complete track layout from MMC TOC files...",
            )
        )
        try:
            toc = self._toc_path.read_bytes()
            full_toc = self._full_toc_path.read_bytes()
        except OSError as exc:
            raise LayoutError(f"Could not read MMC TOC file: {exc}") from exc
        return self._parse(toc, full_toc)

    @staticmethod
    def _parse(toc, full_toc):
        try:
            parsed = parse_mmc_toc(toc)
            parsed = apply_session_boundaries(
                parsed, parse_mmc_full_toc(full_toc)
            )
            tracks = tuple(_track(item) for item in parsed)
            return DiscLayout(tracks, tracks[-1].end_lba)
        except RuntimeError as exc:
            raise TocParseError(str(exc)) from exc

    def _capture(self, command, label):
        result = self._runner.capture(
            command,
            text=False,
            merge_stderr=False,
        )
        if result.returncode != 0:
            details = (result.stderr or b"").decode(
                "utf-8",
                errors="replace",
            ).strip()
            raise LayoutError(
                f"{label} failed"
                + ("\n\n" + details if details else "")
            )
        return result


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
