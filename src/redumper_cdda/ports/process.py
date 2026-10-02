"""Process-execution port."""

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence


Command = Sequence[str]
LineObserver = Callable[[str], None]


@dataclass(frozen=True)
class CommandResult:
    command: tuple
    returncode: int
    output: object = None
    stderr: object = None


class ProcessRunner(Protocol):
    def capture(
        self,
        command: Command,
        text: bool = True,
        merge_stderr: bool = False,
    ) -> CommandResult:
        ...

    def run(self, command: Command, inherit_output: bool = False) -> CommandResult:
        ...

    def run_streaming(
        self,
        command: Command,
        observer: Optional[LineObserver] = None,
    ) -> CommandResult:
        ...
