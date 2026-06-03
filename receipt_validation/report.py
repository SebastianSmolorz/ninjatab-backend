"""CSV reporting for the validation pipeline.

One file per named run, overwritten on re-run. Rows: one per (strategy, case)
plus an ``OVERALL`` row per strategy (the mean of its per-case aggregates), so a
single file supports both cross-strategy comparison and per-receipt debugging.
"""

import csv

# Score columns, in display order. Each maps to a ``mean_<key>`` in the per-case
# aggregate; the OVERALL row averages these across cases.
_SCORE_COLUMNS = [
    ("weighted_score", "mean_total_score"),
    ("receipt_total", "mean_receipt_total_accuracy"),
    ("items_total", "mean_items_total_accuracy"),
    ("item_count", "mean_item_count_match"),
    ("sum_vs_receipt", "mean_items_sum_vs_receipt_total"),
    ("sum_vs_items", "mean_items_sum_vs_items_total"),
    ("name_fuzzy", "mean_item_name_fuzzy_match"),
    ("translated_fuzzy", "mean_item_translated_name_fuzzy_match"),
    ("currency", "mean_currency_match"),
    ("language", "mean_language_match"),
    ("date", "mean_date_match"),
    ("stability", "stability_score"),
    ("success", "success_rate"),
    ("blocking_p50_ms", "blocking_p50_ms"),
    ("blocking_p95_ms", "blocking_p95_ms"),
    ("blocking_mean_ms", "blocking_mean_ms"),
    ("api_mean_ms", "api_mean_ms"),
]

_HEADER = ["run_at", "strategy", "version", "model", "scope", "runs"] + [
    col for col, _ in _SCORE_COLUMNS
]


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _row(run_at, name, version, model, scope, runs, agg) -> list:
    return [run_at, name, version, model, scope, runs] + [
        _fmt(agg.get(key)) for _, key in _SCORE_COLUMNS
    ]


def write_csv(results: dict, path) -> None:
    """Write the pipeline ``results`` to ``path`` (overwriting)."""
    run_at = results.get("run_at", "")
    cases = results.get("cases", {})

    # Collect rows per strategy so OVERALL can follow that strategy's case rows.
    per_strategy: dict[str, dict] = {}
    for case_data in cases.values():
        scope = case_data.get("receipt_establishment_name") or "?"
        for name, sdata in case_data["strategies"].items():
            agg = sdata.get("aggregate") or {}
            runs = len(sdata.get("runs") or [])
            entry = per_strategy.setdefault(
                name,
                {
                    "version": sdata.get("version", ""),
                    "model": sdata.get("model", ""),
                    "case_rows": [],
                    "aggs": [],
                    "runs": 0,
                },
            )
            entry["case_rows"].append((scope, runs, agg))
            entry["aggs"].append(agg)
            entry["runs"] += runs

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_HEADER)
        for name, entry in per_strategy.items():
            for scope, runs, agg in entry["case_rows"]:
                writer.writerow(
                    _row(run_at, name, entry["version"], entry["model"], scope, runs, agg)
                )
            overall = {
                key: _mean_over([a.get(key) for a in entry["aggs"]])
                for _, key in _SCORE_COLUMNS
            }
            writer.writerow(
                _row(run_at, name, entry["version"], entry["model"], "OVERALL", entry["runs"], overall)
            )


def _mean_over(values):
    nums = [v for v in values if v is not None]
    return sum(nums) / len(nums) if nums else None
