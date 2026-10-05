"""Temporary extraction workspace adapter."""

import re
import shutil
import stat as stat_module
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..domain.events import LifecycleEvent
from ..domain.errors import DumpError


PRIMARY_DUMP_SUFFIXES = frozenset(
    {
        ".atip",
        ".cache",
        ".cdtext",
        ".fulltoc",
        ".pma",
        ".scram",
        ".scrap",
        ".state",
        ".subcode",
        ".toc",
    }
)


@dataclass(frozen=True)
class Workspace:
    path: Path

    def snapshot(self):
        result = {}
        for path in self.path.iterdir():
            try:
                stat = path.stat()
                resolved = path.resolve()
            except OSError:
                continue
            if not stat_module.S_ISREG(stat.st_mode):
                continue
            result[resolved] = (stat.st_size, stat.st_mtime_ns)
        return result

    def changed_files(self, before):
        result = []
        for path in self.path.iterdir():
            try:
                stat = path.stat()
                resolved = path.resolve()
            except OSError:
                continue
            if not stat_module.S_ISREG(stat.st_mode):
                continue
            current = (stat.st_size, stat.st_mtime_ns)
            if resolved not in before or before[resolved] != current:
                result.append(path)
        return result

    def stage_existing_dump(self, source_prefix, image_name):
        """Copy one redumper CD dump set into this disposable workspace."""

        source_prefix = Path(source_prefix)
        source_name = source_prefix.name
        if not source_name:
            raise DumpError("The existing dump path must include a filename prefix.")
        try:
            candidates = tuple(source_prefix.parent.iterdir())
        except OSError as exc:
            raise DumpError(
                f"Could not read existing dump directory {source_prefix.parent}: {exc}"
            ) from exc

        copied_suffixes = set()
        copied = []
        for source in candidates:
            if not source.is_file() or not source.name.startswith(f"{source_name}."):
                continue
            relative = source.name[len(source_name):]
            suffix = source.suffix.lower()
            if suffix not in PRIMARY_DUMP_SUFFIXES and not re.fullmatch(
                r"\.\d+\.cache", relative, re.IGNORECASE
            ):
                continue
            target = self.path / f"{image_name}{relative}"
            try:
                shutil.copy2(source, target)
                target.chmod(target.stat().st_mode | stat_module.S_IWUSR)
            except OSError as exc:
                raise DumpError(
                    f"Could not copy existing dump file {source}: {exc}"
                ) from exc
            copied.append(target)
            copied_suffixes.add(relative.lower())

        required = {".state", ".subcode", ".toc"}
        missing = sorted(required - copied_suffixes)
        if not ({".scram", ".scrap"} & copied_suffixes):
            missing.append(".scram (or .scrap)")
        if missing:
            missing_text = ", ".join(missing)
            raise DumpError(
                "Existing dump is incomplete; missing "
                f"{missing_text}. Supply the dump path without an extension."
            )
        return tuple(copied)


class TemporaryWorkspaceFactory:
    def __init__(self, reporter, temporary_directory=tempfile.TemporaryDirectory):
        self._reporter = reporter
        self._temporary_directory = temporary_directory

    @contextmanager
    def create(self):
        report_removed = lambda: None
        try:
            with self._temporary_directory(prefix="redumper-cdda-") as value:
                workspace = Workspace(Path(value))
                report_removed = lambda: self._reporter.publish(
                    LifecycleEvent("workspace_removed", workspace.path)
                )
                self._reporter.publish(
                    LifecycleEvent("workspace_created", workspace.path)
                )
                yield workspace
        finally:
            report_removed()
