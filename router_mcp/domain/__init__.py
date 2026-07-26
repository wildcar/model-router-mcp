"""Pure domain types shared across adapters, routing, accounting, mcp and web.

This package must not import from adapters/storage/mcp/web — dependencies point
inward, towards these types, to keep the import graph acyclic.
"""

from router_mcp.domain.errors import (
    AccessDenied,
    BadRequest,
    Conflict,
    ModelRetired,
    ModelUnavailable,
    NotConfigured,
    NotFound,
    ProviderDisabled,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    ResponseTruncated,
    RouterError,
    UnknownModel,
)
from router_mcp.domain.messages import Attachment, RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality

__all__ = [
    "Modality",
    "Attachment",
    "RouterRequest",
    "RouterResponse",
    "Usage",
    "RouterError",
    "BadRequest",
    "Conflict",
    "UnknownModel",
    "ModelRetired",
    "ModelUnavailable",
    "ProviderUnavailable",
    "ProviderTimeout",
    "RateLimited",
    "ProviderError",
    "ResponseTruncated",
    "AccessDenied",
    "NotConfigured",
    "NotFound",
    "ProviderDisabled",
]
