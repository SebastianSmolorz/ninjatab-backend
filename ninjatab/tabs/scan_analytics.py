"""Reusable receipt-scan outcome analytics.

All emission is best-effort: failures here must never propagate to the caller.
"""
import logging

from ninjatab.utilities.analytics import safe_capture

logger = logging.getLogger("app")

VALID_OUTCOMES = {"success", "edited", "mismatch", "rescanned", "abandoned"}


def compute_submit_outcome(was_edited: bool, had_mismatch: bool) -> str:
    if was_edited:
        return "edited"
    if had_mismatch:
        return "mismatch"
    return "success"


def fire_scan_outcome(user_uuid, scan_session_id, outcome, tab_id=None, bill_id=None):
    try:
        if outcome not in VALID_OUTCOMES:
            logger.warning("Ignoring invalid scan outcome=%s session=%s", outcome, scan_session_id)
            return
        properties = {
            "scan_session_id": str(scan_session_id) if scan_session_id else None,
            "outcome": outcome,
        }
        if tab_id is not None:
            properties["tab_id"] = str(tab_id)
        if bill_id is not None:
            properties["bill_id"] = str(bill_id)
        safe_capture(user_uuid, "receipt_scan_outcome", properties=properties)
    except Exception:
        logger.exception("fire_scan_outcome failed session=%s outcome=%s", scan_session_id, outcome)
