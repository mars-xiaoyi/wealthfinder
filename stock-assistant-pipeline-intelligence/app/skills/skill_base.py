from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import BaseModel

from app.llm.errors import LLMError


@dataclass
class SkillInput:
    """Base for each Skill's input."""


class SkillResult(BaseModel):
    """Base for each Skill's result."""


@dataclass
class LlmCallMetric:
    call_index: int
    purpose: str  # "structured_output"
    input_tokens: int
    output_tokens: int
    latency_ms: int


@dataclass
class LlmErrorEntry:
    call_index: int
    error_type: str  # an LLMError class name, or "BusinessValidationError"
    error_detail: str
    retried: bool


class SkillBase[I: SkillInput, R: SkillResult](ABC):
    """One LLM-backed task. Construct a fresh instance per run() call."""

    def __init__(self, input: I) -> None:
        self._input = input
        self._llm_calls: list[LlmCallMetric] = []
        self._llm_errors: list[LlmErrorEntry] = []

    async def run(self) -> R:
        """Run the Skill. A failed LLM call is returned as a failed result, not raised."""
        try:
            return await self.llm_process()
        except LLMError as err:
            self._llm_errors.append(
                LlmErrorEntry(
                    call_index=len(self._llm_calls) + 1,
                    error_type=type(err).__name__,
                    error_detail=err.dev_message,
                    retried=False,
                )
            )
            return self.build_fail_result(err)

    @abstractmethod
    async def llm_process(self) -> R:
        """Return the Skill's result. Raises an LLMError if an LLM call fails."""

    @abstractmethod
    def build_fail_result(self, err: LLMError) -> R:
        """Build the result for a run that failed with err."""
