"""Pure domain types shared across adapters, routing, accounting, mcp and web.

This package must not import from adapters/storage/mcp/web — dependencies point
inward, towards these types, to keep the import graph acyclic.
"""

from router_mcp.domain.errors import (
    AccessDenied,
    BadRequest,
    Conflict,
    ContentFiltered,
    InsufficientCredits,
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
from router_mcp.domain.task import (
    TASKS,
    BillingGroup,
    Media,
    Task,
    TaskSignature,
    media_of,
    resolve_task,
    tasks_for,
    validate_task_inputs,
)

__all__ = [
    "Modality",
    "BillingGroup",
    "Media",
    "Task",
    "TaskSignature",
    "TASKS",
    "tasks_for",
    "media_of",
    "resolve_task",
    "validate_task_inputs",
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
    "ContentFiltered",
    "InsufficientCredits",
    "AccessDenied",
    "NotConfigured",
    "NotFound",
    "ProviderDisabled",
]
