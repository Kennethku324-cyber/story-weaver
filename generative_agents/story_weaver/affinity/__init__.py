"""story_weaver.affinity — 關係／好感度系統公開 API（spec §3.1）。"""

from .api import affinity_bp
from .gm import (
    GMAdjustmentItem,
    GMAdjustmentResponse,
    apply_gm_response,
    build_gm_prompt,
)
from .memory import initial_poignancy, inject_change, inject_initial
from .models import (
    AFFINITY_MAX,
    AFFINITY_MIN,
    BANDS,
    DELTA_CLAMP,
    LABEL_MAX_LEN,
    AffinityChange,
    AffinityEntry,
    RelationInput,
    SetupAffinityPayload,
    SetupAffinityResult,
    SetupErrorItem,
)
from .store import (
    AffinityStore,
    SetupValidationError,
    UnknownAgentError,
    build_matrix_from_setup,
    validate_setup,
)

__all__ = [
    "AFFINITY_MAX",
    "AFFINITY_MIN",
    "BANDS",
    "DELTA_CLAMP",
    "LABEL_MAX_LEN",
    "AffinityChange",
    "AffinityEntry",
    "AffinityStore",
    "GMAdjustmentItem",
    "GMAdjustmentResponse",
    "RelationInput",
    "SetupAffinityPayload",
    "SetupAffinityResult",
    "SetupErrorItem",
    "SetupValidationError",
    "UnknownAgentError",
    "affinity_bp",
    "apply_gm_response",
    "build_gm_prompt",
    "build_matrix_from_setup",
    "initial_poignancy",
    "inject_change",
    "inject_initial",
    "validate_setup",
]
