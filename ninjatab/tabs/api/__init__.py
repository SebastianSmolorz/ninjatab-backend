"""The API routers, split by resource. urls.py mounts them."""
from .bills import bill_router
from .groups import group_router
from .receipts import receipt_router
from .tabs import tab_router

__all__ = ["bill_router", "group_router", "receipt_router", "tab_router"]
