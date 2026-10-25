"""Deprecated shim — import from ``app.services.sandbox`` instead.

Kept for one release so out-of-tree callers keep working; nothing in this repository
imports it. The ``Daytona*`` error names are aliases of the provider-neutral classes.
``DaytonaRuntime.signed_preview_url`` was removed (it had no callers; the editor uses the
proxy's stable alias host), and ``provision`` now takes the ``Workspace`` row.
"""

from app.services.sandbox.base import (
    CommandTimeoutError as DaytonaCommandTimeoutError,
)
from app.services.sandbox.base import (
    ProvisioningError as DaytonaProvisioningError,
)
from app.services.sandbox.base import (
    RuntimeCommandResult,
    RuntimePreviewLink,
)
from app.services.sandbox.base import (
    RuntimeUnavailableError as DaytonaUnavailableError,
)
from app.services.sandbox.base import (
    SandboxNotFoundError as DaytonaSandboxNotFoundError,
)
from app.services.sandbox.base import (
    SandboxRuntimeError as DaytonaRuntimeError,
)
from app.services.sandbox.daytona import DaytonaRuntime, map_daytona_error
from app.services.sandbox.factory import get_runtime_by_name


def get_daytona_runtime() -> DaytonaRuntime:
    return get_runtime_by_name("daytona")  # type: ignore[return-value]


__all__ = [
    "DaytonaCommandTimeoutError",
    "DaytonaProvisioningError",
    "DaytonaRuntime",
    "DaytonaRuntimeError",
    "DaytonaSandboxNotFoundError",
    "DaytonaUnavailableError",
    "RuntimeCommandResult",
    "RuntimePreviewLink",
    "get_daytona_runtime",
    "map_daytona_error",
]
