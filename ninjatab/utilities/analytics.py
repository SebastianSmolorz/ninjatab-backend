import logging

import sentry_sdk
from posthog import new_context, identify_context, capture as _ph_capture

logger = logging.getLogger("app")


def safe_capture(distinct_id, event, properties=None):
    """Send a PostHog event without raising. Reports failures to Sentry."""
    try:
        with new_context():
            identify_context(str(distinct_id) if distinct_id is not None else "$anon")
            _ph_capture(event, properties=properties)
    except Exception as e:
        logger.exception("Failed to send analytics event %s", event)
        try:
            sentry_sdk.capture_exception(e)
        except Exception:
            logger.exception("Failed to report analytics failure to Sentry")


def safe_identify(distinct_id, properties=None, set_once=None):
    """Send a PostHog $identify event so the person is marked $is_identified=true."""
    if distinct_id is None:
        return
    try:
        with new_context():
            identify_context(str(distinct_id))
            event_props = {}
            if properties:
                event_props["$set"] = properties
            if set_once:
                event_props["$set_once"] = set_once
            _ph_capture("$identify", properties=event_props)
    except Exception as e:
        logger.exception("Failed to send analytics $identify for %s", distinct_id)
        try:
            sentry_sdk.capture_exception(e)
        except Exception:
            logger.exception("Failed to report analytics failure to Sentry")
