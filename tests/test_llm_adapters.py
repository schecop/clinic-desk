"""Provider quirks normalised in the adapters (no network: parse() on recorded-shape bodies)."""

from __future__ import annotations

import asyncio

from clinic_desk.clinic import load_clinic
from clinic_desk.llm.base import LLMError
from clinic_desk.llm.openai_compat import OpenAICompatClient, split_thought
from clinic_desk.llm.scripted import ScriptedLLM, say
from clinic_desk.simulator.persona import load_personas
from clinic_desk.simulator.runner import RoleModel, run_conversation

GEMMA = (
    "<thought>*   Role: patient.\n    *   One sentence? Yes.</thought>"
    "Hola, buenos días, quería pedir cita."
)


def test_split_thought() -> None:
    assert split_thought(GEMMA) == (
        "Hola, buenos días, quería pedir cita.",
        "*   Role: patient.\n    *   One sentence? Yes.",
    )
    assert split_thought("Hola.") == ("Hola.", "")
    assert split_thought("<thought>cut by max_tok") == ("", "cut by max_tok")


def test_the_adapter_strips_reasoning_from_the_text_and_the_replayed_turn() -> None:
    client = OpenAICompatClient(
        provider="gemini", model="gemma-4-31b-it", api_key="x", base_url="http://unused"
    )
    body = {
        "choices": [{"message": {"role": "assistant", "content": GEMMA}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 9},
    }
    r = client.parse(body, 10.0)
    assert r.text == "Hola, buenos días, quería pedir cita."
    assert r.message.raw["content"] == r.text
    asyncio.run(client.aclose())


class _Failing:
    provider, model = "gemini", "gemma"

    async def complete(self, **_: object) -> object:
        raise LLMError("after 4 attempts: ReadTimeout")

    async def aclose(self) -> None:
        return None


def test_a_persona_api_failure_ends_that_conversation_only() -> None:
    persona = next(p for p in load_personas(load_clinic()) if p.id == "minors-alone")
    c = asyncio.run(
        run_conversation(
            persona,
            receptionist=RoleModel(ScriptedLLM([say("unused")])),
            persona_model=RoleModel(_Failing()),  # type: ignore[arg-type]
        )
    )
    assert c.end == "error" and c.error is not None and c.error.startswith("persona:")
    assert not c.counted
