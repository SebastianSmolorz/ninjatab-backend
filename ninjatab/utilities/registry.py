"""Registry of application Options.

Each entry maps an option name to the ``defaults`` used when the option is first
created — either by the ``sync_options`` management command or by
``ensure_option`` at runtime. Syncing uses get_or_create, so values that have
since been changed in the database are never overwritten.
"""

# Option names
SCAN_STRATEGY = "scan_strategy"

OPTION_REGISTRY = {
    SCAN_STRATEGY: {
        "active": True,
        "value": "baseline_mistral_ocr",
    },
}


def ensure_option(name: str):
    """Return the Option for ``name``, creating it from the registry defaults
    if it does not yet exist (get_or_create)."""
    from .models import Option

    option, _ = Option.objects.get_or_create(
        name=name, defaults=OPTION_REGISTRY[name]
    )
    return option
