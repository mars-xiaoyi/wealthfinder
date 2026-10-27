from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class GenerationConfig:
    """Per-call generation settings."""

    model: str
    temperature: float
    max_output_tokens: int


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class StructuredLLMResponse[T: BaseModel]:
    """One structured-output call's validated output and its metrics."""

    output: T
    input_tokens: int
    output_tokens: int  # visible output plus thinking tokens, both billed as output
    latency_ms: int


class LLMAdapter(ABC):
    """Provider-agnostic LLM interface."""

    @abstractmethod
    async def generate_structured[T: BaseModel](
        self,
        messages: list[Message],
        output_schema: type[T],
        generation_config: GenerationConfig,
    ) -> StructuredLLMResponse[T]:
        """
        Make one LLM call whose response is constrained to output_schema.
        Raises LLMRateLimitError, LLMProviderError, LLMRequestError, LLMAuthenticationError or
        LLMSchemaViolationError.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release the provider client's resources."""
