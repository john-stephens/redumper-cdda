"""Temporary extraction workspace adapter."""

import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..domain.events import LifecycleEvent


@dataclass(frozen=True)
class Workspace:
    path: Path

    def snapshot(self):
        result = {}
        for path in self.path.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            result[path.resolve()] = (stat.st_size, stat.st_mtime_ns)
        return result

    def changed_files(self, before):
        result = []
        for path in self.path.iterdir():
            if not path.is_file():
                continue
            resolved = path.resolve()
            try:
                stat = path.stat()
            except OSError:
                continue
            current = (stat.st_size, stat.st_mtime_ns)
            if resolved not in before or before[resolved] != current:
                result.append(path)
        return result


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
