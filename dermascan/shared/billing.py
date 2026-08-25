"""
Deprecated shim — import from ``dermascan.billing`` instead.

Kept so existing ``dermascan.shared.billing`` imports keep working until
the Paystack branch merge consolidates entry points.
"""

from dermascan.billing import *  # noqa: F403
from dermascan.billing import __all__ as __all__
