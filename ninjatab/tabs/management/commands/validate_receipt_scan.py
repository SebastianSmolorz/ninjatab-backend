import json
from pathlib import Path

from django.core.management.base import BaseCommand

from receipt_validation.strategies import STRATEGIES, STRATEGIES_BY_NAME
from receipt_validation.runner import run_pipeline


class Command(BaseCommand):
    help = "Run receipt scan validation pipeline across strategies and test cases"

    def add_arguments(self, parser):
        parser.add_argument(
            "--cases",
            default="all",
            help='Comma-separated case UUIDs or "all" (default: all)',
        )
        parser.add_argument(
            "--strategies",
            default="all",
            help=f'Comma-separated strategy names or "all". Available: {", ".join(STRATEGIES_BY_NAME)}',
        )
        parser.add_argument(
            "--runs",
            type=int,
            default=1,
            help="Number of runs per strategy per case (default: 1)",
        )
        parser.add_argument(
            "--sleep",
            type=int,
            default=0,
            help="Seconds to sleep between runs (default: 0)",
        )
        parser.add_argument(
            "--output",
            default=None,
            help="Path to save full JSON output",
        )

    def handle(self, *args, **options):
        case_uuids = None if options["cases"] == "all" else options["cases"].split(",")
        strategy_names = None if options["strategies"] == "all" else options["strategies"].split(",")
        runs = options["runs"]

        if strategy_names:
            unknown = [n for n in strategy_names if n not in STRATEGIES_BY_NAME]
            if unknown:
                self.stderr.write(self.style.ERROR(f"Unknown strategies: {', '.join(unknown)}"))
                return

        self.stdout.write(f"Running pipeline: cases={options['cases']}, strategies={options['strategies']}, runs={runs}")

        results = run_pipeline(
            case_uuids=case_uuids,
            strategy_names=strategy_names,
            runs_per_strategy=runs,
            strategies=STRATEGIES,
            sleep_between_runs=options["sleep"],
        )

        if not results["cases"]:
            self.stdout.write(self.style.WARNING("No cases found. Add images + expected.json to receipt_validation/cases/{uuid}/"))
            return

        self._print_summary(results)

        if options["output"]:
            output_path = Path(options["output"])
            output_path.write_text(json.dumps(results, indent=2))
            self.stdout.write(self.style.SUCCESS(f"Full results saved to {output_path}"))

    def _print_summary(self, results: dict):
        self.stdout.write("")
        self.stdout.write("=" * 80)
        self.stdout.write("SUMMARY")
        self.stdout.write("=" * 80)

        score_keys = [
            ("mean_total_score", "TOTAL"),
            ("mean_receipt_total_accuracy", "Total Acc"),
            ("mean_items_total_accuracy", "Items Acc"),
            ("mean_item_count_match", "Count"),
            ("mean_items_sum_vs_receipt_total", "Sum/Receipt"),
            ("mean_items_sum_vs_items_total", "Sum/Items"),
            ("mean_item_name_fuzzy_match", "Name"),
            ("mean_item_translated_name_fuzzy_match", "Translated"),
            ("mean_currency_match", "Currency"),
            ("mean_language_match", "Language"),
            ("mean_date_match", "Date"),
            ("stability_score", "Stability"),
            ("success_rate", "Success"),
        ]
        header = f"{'Establishment':<20}  {'Strategy':<28}  " + "  ".join(f"{label:<10}" for _, label in score_keys)
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        strategy_scores: dict[str, dict[str, list[float]]] = {}
        for case_uuid, case_data in results["cases"].items():
            establishment = (case_data.get("receipt_establishment_name") or case_uuid)[:20]
            for strategy_name, strategy_data in case_data["strategies"].items():
                agg = strategy_data["aggregate"]
                scores_str = "  ".join(
                    f"{agg.get(key, 0.0) or 0.0:.2f}      " for key, _ in score_keys
                )
                self.stdout.write(f"{establishment:<20}  {strategy_name:<28}  {scores_str}")
                bucket = strategy_scores.setdefault(strategy_name, {})
                for key, _ in score_keys:
                    v = agg.get(key)
                    if v is not None:
                        bucket.setdefault(key, []).append(v)

        if len(results["cases"]) > 1:
            self.stdout.write("-" * len(header))
            for strategy_name, buckets in strategy_scores.items():
                overall_str = "  ".join(
                    f"{sum(buckets[key]) / len(buckets[key]):.2f}      " if key in buckets else f"{'n/a':<12}"
                    for key, _ in score_keys
                )
                self.stdout.write(f"{'OVERALL':<20}  {strategy_name:<28}  {overall_str}")

        self.stdout.write("=" * 80)
