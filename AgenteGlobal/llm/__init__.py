"""Provider assíncrono e contratos de modelo do AgenteGlobal."""

from .base import ModelAdapter, ModelRequest
from .capabilities import CapabilityEvidence, CapabilityStatus, ModelCapabilities
from .contracts import ModelCapabilityError, ModelResponse, ModelStreamEvent, RuntimeResult, ToolCall
from .huawei_maas import HuaweiMaaSAdapter, HuaweiMaaSConfig
from .reasoning import ReasoningMode, ReasoningPolicy

__all__ = [
    "CapabilityEvidence",
    "CapabilityStatus",
    "HuaweiMaaSAdapter",
    "HuaweiMaaSConfig",
    "ModelAdapter",
    "ModelCapabilityError",
    "ModelCapabilities",
    "ModelRequest",
    "ModelResponse",
    "ModelStreamEvent",
    "ReasoningMode",
    "ReasoningPolicy",
    "RuntimeResult",
    "ToolCall",
]
