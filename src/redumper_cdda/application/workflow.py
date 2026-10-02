"""Extraction use-case coordinator."""

from ..domain.events import LifecycleEvent
from ..domain.extraction import ExtractionResult
from ..domain.errors import IntegrityStatusError, OutputError, VerificationError


class ExtractionApplication:
    def __init__(
        self,
        layout_provider,
        planner,
        acquirer,
        splitter,
        output_service,
        verifier_factory,
        workspace_factory,
        reporter,
    ):
        self._layout_provider = layout_provider
        self._planner = planner
        self._acquirer = acquirer
        self._splitter = splitter
        self._output_service = output_service
        self._verifier_factory = verifier_factory
        self._workspace_factory = workspace_factory
        self._reporter = reporter

    def read_layout(self, device):
        return self._layout_provider.read(device)

    def run(self, request):
        with self._workspace_factory.create() as workspace:
            disc = self._layout_provider.read(request.device)
            plan = self._planner.create(request, disc, workspace.path)
            verifier = self._verifier_factory(request.accuraterip)
            verifier.prepare(plan)
            self._reporter.publish(
                LifecycleEvent("plan", {"plan": plan, "request": request})
            )
            before = workspace.snapshot()
            acquisition = self._acquirer.acquire(plan, request)
            split = self._splitter.split(
                plan, request, acquisition, before, workspace.changed_files
            )
            for item in split.omitted:
                self._reporter.publish(LifecycleEvent("omitted", item))
            self._reporter.publish(
                LifecycleEvent("output_started", {"outputs": split.outputs})
            )
            try:
                outputs = self._output_service.create(split.outputs)
            except BaseException as exc:
                self._reporter.publish(LifecycleEvent("output_finished", False))
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                if isinstance(exc, OutputError):
                    raise
                raise OutputError(f"output creation failed: {exc}") from exc
            self._reporter.publish(LifecycleEvent("output_finished", True))
            verification = None
            if request.accuraterip and verifier.enabled:
                self._reporter.publish(LifecycleEvent("verification_started"))
                try:
                    verification = verifier.verify(plan, split, workspace.path)
                except VerificationError as exc:
                    raise VerificationError(
                        f"AccurateRip verification failed: {exc}\n"
                        "Completed output files were retained."
                    ) from exc
                self._reporter.publish(
                    LifecycleEvent("verification_report", verification)
                )
            for output in split.outputs:
                self._reporter.publish(LifecycleEvent("output_detail", output))
            result = ExtractionResult(
                plan, acquisition, outputs, verification, split.omitted
            )
            self._reporter.publish(LifecycleEvent("complete", result))
            if split.omitted:
                numbers = ", ".join(
                    f"{item.plan.track.number:02d}" for item in split.omitted
                )
                plural = "" if len(split.omitted) == 1 else "s"
                raise IntegrityStatusError(
                    "--abort-on-skip omitted output for "
                    f"Track{plural} {numbers} because unresolved SCSI/C2 errors "
                    "remain. Clean track files were retained."
                )
            return result
