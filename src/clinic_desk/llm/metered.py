"""An `LLMClient` wrapper that records every call: role, model, tokens, cost, latency.

The simulator wraps each role's client (receptionist, translator, persona, judge) with one, so a
conversation knows what it cost and how slow it was, and the cost ledger and the traces read the
same records. Prices come from config (`usd_per_mtok_in/out`), never from code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from clinic_desk.llm.base import LLMClient, LLMResponse, Message, ToolSpec

MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class Call:
    role: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: Decimal
    latency_ms: float
    stop: str


class Metered:
    def __init__(
        self,
        inner: LLMClient,
        role: str,
        usd_per_mtok_in: Decimal = Decimal(0),
        usd_per_mtok_out: Decimal = Decimal(0),
        calls: list[Call] | None = None,
    ) -> None:
        self.inner = inner
        self.role = role
        self.provider = inner.provider
        self.model = inner.model
        self._in, self._out = usd_per_mtok_in, usd_per_mtok_out
        self.calls: list[Call] = calls if calls is not None else []  # may be shared by roles

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = 1024,
    ) -> LLMResponse:
        response = await self.inner.complete(
            system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )
        usd = (response.input_tokens * self._in + response.output_tokens * self._out) / MTOK
        self.calls.append(
            Call(
                self.role,
                self.provider,
                self.model,
                response.input_tokens,
                response.output_tokens,
                usd,
                response.latency_ms,
                response.stop,
            )
        )
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()
