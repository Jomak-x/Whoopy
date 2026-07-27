"""Public capability contracts implemented by concrete Whoopy adapters."""

from whoopy.ports.errors import (
    AdapterError,
    FatalAdapterError,
    InvalidAdapterOutput,
    TransientAdapterError,
)
from whoopy.ports.models import (
    AdapterMetadata,
    ScriptGenerationRequest,
    ScriptGenerationResult,
    ScriptGenerator,
    SpeechSynthesizer,
)

__all__ = [
    "AdapterError",
    "AdapterMetadata",
    "FatalAdapterError",
    "InvalidAdapterOutput",
    "ScriptGenerationRequest",
    "ScriptGenerationResult",
    "ScriptGenerator",
    "SpeechSynthesizer",
    "TransientAdapterError",
]
