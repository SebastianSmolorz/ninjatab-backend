"""Single source of truth for scan strategies: the production registry.

The validation pipeline drives the same strategy classes used in production so
that what we validate is what users get.
"""

from ninjatab.tabs.receipt_scanning.strategies import (  # noqa: F401
    STRATEGIES,
    STRATEGIES_BY_NAME,
)
