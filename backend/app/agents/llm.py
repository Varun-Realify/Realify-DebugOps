"""Direct Claude API calls for agents that only reason over provided inputs (Bug Hunter, Security, Review).

These agents get no tools and no repository access: they judge evidence the platform hands them.
"""

import logging
from dataclasses import dataclass
from typing import Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from app.config import Settings

log = logging.getLogger("debugops.llm")
T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output). Used for cost reporting only; budgets are enforced on tokens.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
    return round((tokens_in * pin + tokens_out * pout) / 1_000_000, 6)


class AgentError(Exception):
    pass


@dataclass
class LLMResult:
    parsed: BaseModel
    tokens_in: int
    tokens_out: int
    cost_usd: float


class LLM(Protocol):
    async def structured(self, *, agent: str, system: str, prompt: str, schema: type[T]) -> LLMResult: ...


class AnthropicLLM:
    def __init__(self, settings: Settings):
        if not settings.anthropic_api_key:
            raise AgentError("ANTHROPIC_API_KEY is not configured")
        self.s = settings
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url,
                                               max_retries=3, timeout=600.0)

    async def structured(self, *, agent: str, system: str, prompt: str, schema: type[T]) -> LLMResult:
        try:
            resp = await self.client.beta.messages.create(
                model=self.s.agent_model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                # Structured outputs: the response is constrained to the schema. Validated again below.
                output_config={"effort": self.s.agent_effort,
                               "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
                # Opt into server-side fallback: a safety-classifier decline is retried on Anthropic's
                # recommended fallback model instead of failing the step.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            raise AgentError(f"{agent}: rate limited by the Claude API") from e
        except anthropic.APIConnectionError as e:
            raise AgentError(f"{agent}: cannot reach the Claude API") from e
        except anthropic.APIStatusError as e:
            raise AgentError(f"{agent}: Claude API error {e.status_code}: {e.message}") from e
        # Check the stop reason before reading content: a declined request has no valid output.
        if resp.stop_reason == "refusal":
            raise AgentError(f"{agent}: the model declined this request")
        if resp.stop_reason == "max_tokens":
            raise AgentError(f"{agent}: response hit max_tokens before completing")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as e:
            raise AgentError(f"{agent}: response did not match the required schema: {str(e)[:300]}") from e
        tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
        return LLMResult(parsed, tin, tout, cost_usd(resp.model, tin, tout))
