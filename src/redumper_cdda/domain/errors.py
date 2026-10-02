"""Typed application errors."""


class RedumperCddaError(RuntimeError):
    """Base class for expected application failures."""


class DomainModelError(RedumperCddaError):
    """A domain value violates its construction invariant."""


class LayoutError(RedumperCddaError):
    """Disc layout acquisition or validation failed."""


class TocParseError(LayoutError):
    """An external TOC representation could not be parsed."""


class LayoutMismatchError(LayoutError):
    """Independent layout sources disagree."""


class SelectionError(RedumperCddaError):
    """A requested track selection is invalid."""


class PlanningError(RedumperCddaError):
    """A valid extraction plan cannot be constructed."""


class ToolExecutionError(RedumperCddaError):
    """An external tool failed."""


class DumpError(ToolExecutionError):
    """The bounded redumper dump failed."""


class RefineError(ToolExecutionError):
    """A bounded redumper refine pass failed."""


class SplitError(ToolExecutionError):
    """The forced partial-image split failed."""


class IntegrityStatusError(RedumperCddaError):
    """Media integrity could not be determined safely."""


class CueError(RedumperCddaError):
    """Generated CUE or split-source resolution failed."""


class OutputError(RedumperCddaError):
    """Output creation or commit failed."""


class AudioOutputError(OutputError):
    """Audio output creation failed."""


class IsoOutputError(OutputError):
    """ISO output creation failed."""


class VerificationError(RedumperCddaError):
    """Post-extraction verification failed."""


class DependencyError(RedumperCddaError):
    """A required external executable is unavailable."""


class TerminationRequested(RedumperCddaError):
    """The process received a termination signal."""

    def __init__(self, signum, signal_name):
        self.signum = signum
        self.signal_name = signal_name
        super().__init__(signal_name)
