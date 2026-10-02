# Architecture

## Overview

`redumper-cdda` uses a ports-and-adapters architecture with immutable domain
models and constructor-injected services. The design keeps sector arithmetic
and extraction policy visible while separating them from subprocess control,
filesystem access, console formatting, and ARver.

The dependency direction is inward:

``` text
CLI and bootstrap
        |
        v
system adapters ---> ports <--- application services
                          \       /
                           domain
```

The domain does not know about command-line arguments, subprocesses, files,
redumper, cdparanoia, `sg_raw`, ARver, or console output. Application services
coordinate domain values through narrow ports. Adapters implement those ports.
`bootstrap.py` is the only production composition root.

This structure is an architectural boundary, not permission to obscure the
extraction rules. Logical and physical LBA ranges remain explicit domain
values, and exact external command arrays remain independently testable.

## Package responsibilities

### Domain

`src/redumper_cdda/domain/` contains immutable values, results, lifecycle
events, and typed errors:

-   `disc.py`: `Track`, `TrackKind`, `DiscLayout`, and `AudioLayout`;
-   `extraction.py`: requests, selections, sector ranges, plans, and extraction
    results;
-   `integrity.py`: disc- and track-level media-error values and write offsets;
-   `outputs.py`: output plans, resolved sources, completed outputs, and
    verification results;
-   `events.py`: structured lifecycle events passed to reporters;
-   `errors.py`: the expected application exception hierarchy.

Domain models are frozen dataclasses. A phase receives immutable inputs and
returns a new immutable result; there is no mutable global run context.

`ExtractionPlan` is the authoritative expression of extraction boundaries. It
enforces:

``` text
physical_range.start_lba == logical_range.start_lba
physical_range.end_lba   == logical_range.end_lba + 1
```

The one-sector physical padding belongs only to redumper dump/refine commands.
Output assembly consumes resolved logical segments and cannot include that
padding accidentally.

### Application services

`src/redumper_cdda/application/` implements use cases and policy:

-   `planning.py`: resolves selection rules, Track 0, logical and physical
    ranges, output plans, image names, and exact command plans;
-   `acquisition.py`: performs one bounded dump and only the necessary bounded
    refinement passes;
-   `splitting.py`: performs one forced split, applies strict error filtering,
    and resolves typed audio/data sources from the generated CUE;
-   `output.py`: plans output names and commits complete output sets
    transactionally through writer strategies;
-   `workflow.py`: `ExtractionApplication`, the sequencing-only coordinator.

Application services may depend on domain modules and port protocols. They do
not format console output, call `sys.exit`, construct subprocesses, parse CLI
arguments, or import concrete system adapters.

`ExtractionApplication.run()` executes the phases in this order:

``` text
temporary workspace
    |
complete reconciled layout
    |
typed extraction plan
    |
verifier preflight
    |
bounded dump and conditional bounded refinement
    |
one force-split and exact source resolution
    |
transactional WAV/ISO creation
    |
optional AccurateRip verification
    |
typed ExtractionResult
```

AccurateRip runs after output commit. A verification lookup or checksum failure
therefore cannot remove completed output. With per-track `--abort-on-skip`, the
application commits every clean permitted output and then returns a nonzero CLI
outcome describing the omitted tracks.

### Ports

`src/redumper_cdda/ports/` defines small `typing.Protocol` boundaries for:

-   layout acquisition;
-   redumper acquisition and integrity inspection;
-   process execution;
-   output writers and output transactions;
-   lifecycle reporting;
-   verification.

Ports describe what the application needs, not an application-wide service
container. New replaceable behavior should normally add or refine a focused
protocol rather than introduce conditionals throughout the coordinator.

### Adapters

`src/redumper_cdda/adapters/` contains concrete integrations:

-   `subprocess_runner.py`: child-process execution and interruption cleanup;
-   `mmc.py` and `cdparanoia.py`: external layout readers;
-   `layout_provider.py`: exact MMC/cdparanoia reconciliation, including the
    validated enhanced-CD session-lead-out exception;
-   `redumper.py`: exact command construction, progress parsing, integrity
    parsing, and state inspection;
-   `cue.py`: CUE parsing, discovery, and audio/data source resolution;
-   `wav.py` and `iso9660.py`: output-writer strategies;
-   `accuraterip.py`: real and null verification strategies;
-   `workspace.py`: unique temporary-workspace lifecycle;
-   `console.py`: quiet, concise, and verbose reporters.

Adapters translate external representations at their boundary and return typed
domain values. Dictionary-shaped external APIs must not leak into application
services or plans.

The top-level `accuraterip.py`, `integrity.py`, `iso9660.py`, `layout.py`, and
`outputs.py` modules contain focused parsing/conversion algorithms used by
adapters. They are implementation helpers, not alternate workflow entry
points. There is intentionally no top-level `workflow.py`, legacy dictionary
adapter, or procedural output-job coordinator.

### CLI and composition

`src/redumper_cdda/cli.py` contains three CLI collaborators:

-   `ArgumentParserFactory` defines syntax and converts track syntax directly
    to `TrackSelection`;
-   `SystemDependencyChecker` validates the external executables required for
    the selected operation;
-   `CliApplication` performs CLI-only validation, creates an
    `ExtractionRequest`, invokes the application, and reports layout-only mode.

The CLI boundary translates typed application errors and interrupts into exit
messages. No deeper layer calls `sys.exit`.

`src/redumper_cdda/bootstrap.py` creates the production object graph. It is the
only place that selects concrete readers, writers, runners, reporters,
workspace management, and verifier implementations. Both the installed entry
point and repository launcher call `redumper_cdda.cli.run`.

## Reporting

Application services and process adapters publish `LifecycleEvent` values.
They never branch on quiet or verbose modes. The reporter chosen by the CLI
formats those events:

-   `QuietReporter` suppresses routine output;
-   `ConciseReporter` renders summaries, progress, warnings, and final counts;
-   `VerboseReporter` renders commands, tool output, exact ranges, resolved
    sources, integrity details, and verification details;
-   `MultiplexReporter` sends the same events to the selected terminal
    reporter and an optional verbose log-file reporter.

Reporting is observational. A reporter must never change selection,
refinement, integrity, output, or verification policy.

## Output transaction

`OutputTransaction` protects existing and newly created outputs:

``` text
ResolvedOutput values
        |
write temporary sibling files
        |
all writers succeed and validate
        |
move existing outputs to backup siblings
        |
commit every new final name
        |
remove backups
```

If writing or committing fails, temporary files are removed and existing
outputs are restored. A separate-output run never exposes a partially committed
new set. Writer selection is keyed by `OutputKind`; adding a new output kind
requires a new strategy and composition-root registration rather than a branch
inside the coordinator.

## Error ownership

Expected failures use the hierarchy in `domain/errors.py`. Responsibility is
split as follows:

-   adapters translate external tool, parsing, and filesystem failures into
    typed errors;
-   application services add phase-specific policy and context;
-   `ExtractionApplication` preserves sequencing and output-retention
    semantics;
-   the CLI is the message and exit-code boundary.

Fail-closed conditions must remain fail closed. In particular, missing media
status is never treated as a clean result, and normal partial split failure is
never used as an SCSI/C2 detector.

## Testing architecture

Tests mirror the architectural ownership:

``` text
tests/
|-- domain/       immutable values and calculations
|-- application/  services with fake ports
|-- adapters/     parsing, exact commands, files, subprocesses, and reporting
|-- contracts/    shared port behavior and production composition
`-- test_cli.py   command-line and exit-boundary behavior
```

Every executable statement and branch must remain covered. Changes should be
tested at the narrowest owning layer, with full workflow tests reserved for
phase ordering, retention, rollback, and cross-service policy.

Hardware validation remains separate from unit coverage. Any extraction change
must also respect the empirical comparison and invariants in `EXTRACTION.md`
and `AGENTS.md`.

## Extension rules

When extending the application:

1. Put immutable concepts and invariants in the domain.
2. Put sequencing and extraction policy in an application service.
3. Define a narrow port when policy needs replaceable external behavior.
4. Implement system-specific behavior in an adapter.
5. Register concrete dependencies only in `bootstrap.py`.
6. Publish structured events; do not print from application services.
7. Translate errors to process exits only in `cli.py`.
8. Keep exact LBA arithmetic and subprocess argument arrays directly
   auditable.

Do not reintroduce mutable job dictionaries, a service locator, a second
workflow entry point, per-track dump loops, or output packaging that performs
additional dump/refine/split commands.
