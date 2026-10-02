"""Output writer port."""

from typing import Protocol

from ..domain.outputs import CompletedOutput, ResolvedOutput


class OutputWriter(Protocol):
    def write(
        self,
        output: ResolvedOutput,
        temporary_path,
        verbose: bool = False,
    ) -> None:
        ...


class OutputService(Protocol):
    def create(
        self,
        outputs: tuple,
        verbose: bool = False,
    ) -> tuple[CompletedOutput, ...]:
        ...
