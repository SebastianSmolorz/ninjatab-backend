"""Score post-processing pipelines against the labels, replaying captured OCR.

Costs no Mistral calls: the OCR is fixed, so two pipelines compared here differ
only in their post-processing and the comparison is paired.
"""

from django.core.management.base import BaseCommand, CommandError

import receipt_validation  # noqa: F401  (puts the repo root on sys.path)
from labeler.evaluation.labels import load_cached_responses, load_labelled_cases
from receipt_validation.replay import (
    PIPELINES,
    evaluate,
    format_report,
    load_captures,
)

DEFAULT_PIPELINE = "strategy:verified_consensus"


class Command(BaseCommand):
    help = (
        "Replay captured OCR through a post-processing pipeline and score it "
        "against the labeller's ground truth. Makes no API calls."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "pipelines", nargs="*", default=None,
            help=f"Pipelines to score (default: {DEFAULT_PIPELINE})",
        )
        parser.add_argument(
            "--compare", default=None,
            help="Score this pipeline first and show the others as deltas against it",
        )
        parser.add_argument(
            "--case", default=None,
            help="Replay one case from the labeller's own cached scan instead, "
                 "showing each candidate and which one selection picked. Works "
                 "on unlabelled cases; scores nothing.",
        )
        parser.add_argument(
            "--list", action="store_true", help="List the available pipelines and exit",
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in PIPELINES:
                self.stdout.write(name)
            return

        if options["case"]:
            self._replay_case(options["case"], options["pipelines"])
            return

        names = list(options["pipelines"] or [DEFAULT_PIPELINE])
        unknown = [n for n in names + [options["compare"]] if n and n not in PIPELINES]
        if unknown:
            raise CommandError(f"Unknown pipeline(s): {', '.join(unknown)}. Try --list.")

        # Loaded once and shared: reading 555 captures per pipeline is the only
        # slow part of this command.
        captures, labels = load_captures(), load_labelled_cases()

        baseline = None
        if options["compare"]:
            baseline = evaluate(options["compare"], captures, labels)
            self.stdout.write(format_report(baseline))
            names = [n for n in names if n != options["compare"]]

        for name in names:
            self.stdout.write(format_report(evaluate(name, captures, labels), baseline))

    def _replay_case(self, filename: str, pipelines: list) -> None:
        """Show how selection treats one case, using the responses the labeller
        cached when it last scanned it."""
        from ninjatab.tabs.receipt_scanning.base import ScanContext
        from ninjatab.tabs.receipt_scanning.postprocess import bill_total, drops_rows
        from ninjatab.tabs.receipt_scanning.strategies import STRATEGIES_BY_NAME

        ocr_results = load_cached_responses(filename)
        if not ocr_results:
            raise CommandError(
                f"No cached scan for {filename}. Scan it once in the labeller first."
            )

        name = (pipelines or ["verified_consensus"])[0].replace("strategy:", "")
        if name not in STRATEGIES_BY_NAME:
            raise CommandError(f"Unknown strategy: {name}")
        ctx = ScanContext(
            image_bytes=b"", content_type="image/jpeg",
            default_currency="USD", tab_id="replay",
        )
        result = STRATEGIES_BY_NAME[name].post_process(ocr_results, ctx)
        metrics = result.metrics

        self.stdout.write(f"{filename}  ({len(ocr_results)} cached calls, strategy {name})")
        gaps = metrics.get("consensus_candidate_gaps") or []
        counts = metrics.get("consensus_candidate_item_counts") or []
        totals = metrics.get("consensus_candidate_items_totals") or []
        selected = metrics.get("consensus_selected_index")
        for i, (count, total, gap) in enumerate(zip(counts, totals, gaps)):
            marker = " <- selected" if i == selected else ""
            gap_text = "n/a" if gap is None else f"{gap:.2f}"
            self.stdout.write(
                f"  call {i}: {count} rows, total {total}, gap {gap_text}{marker}"
            )
        annotation = result.document_annotation or {}
        self.stdout.write(
            f"  method {metrics.get('consensus_selection_method')}; "
            f"bill {bill_total(annotation)} vs receipt_total "
            f"{annotation.get('receipt_total')}; "
            f"model's own items_total {annotation.get('ai_items_total')}"
            + ("  [rows below it]" if drops_rows(annotation) else "")
        )
