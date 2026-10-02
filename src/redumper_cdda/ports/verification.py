"""Post-extraction verification port."""

from typing import Protocol


class Verifier(Protocol):
    enabled: bool

    def prepare(self, plan) -> None:
        ...

    def verify(self, plan, split_result, workdir):
        ...
