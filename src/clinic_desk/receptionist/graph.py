"""The receptionist as a LangGraph graph: one agent node to talk, deterministic nodes to write.

    patient turn ─► route ─┬─ pending proposal ─► confirm ─┬─ yes ───────► act ─► agent
                           │                               └─ no/unclear ─────────► agent
                           └─ nothing pending ────────────────────────────────────► agent
    agent ⇄ tools          (read tools + propose_*; none of them writes)
    tools ─ a proposal was made ─► ask_confirm ─► end of turn
    agent ─ plain reply ─► end of turn

The model never holds a write tool, so "no write without confirmation" is a property of the
graph's edges: `act` is reachable only from `confirm`, and `confirm` only says yes to a plain
yes from the patient, given in reply to a question the graph itself rendered from the exact
pending slot.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from clinic_desk.clinic import Lang, load_clinic
from clinic_desk.llm.base import AssistantMessage, LLMClient, ToolResults, UserMessage
from clinic_desk.receptionist.language import (
    classify_confirmation,
    day_label,
    guess_lang,
    when,
    with_staff,
)
from clinic_desk.receptionist.state import Desk, Outcome, PendingAction, State, new_state
from clinic_desk.receptionist.tools import SPECS, run_tool
from clinic_desk.scheduling.fake import load_seed
from clinic_desk.scheduling.models import Appointment
from clinic_desk.scheduling.scheduler import Scheduler, SchedulingError

PROMPT_PATH = Path("prompts/receptionist.md")

HANDOFF: dict[Lang, str] = {
    "es": "Disculpe, no he podido resolverlo. Le paso con la recepción: llame al {phone}.",
    "ca": "Disculpi, no ho he pogut resoldre. Li passo amb la recepció: truqui al {phone}.",
}
_LANG_NAME: dict[Lang, str] = {"es": "Spanish", "ca": "Catalan"}


# --- rendering -----------------------------------------------------------------------------


def confirmation_question(desk: Desk, pending: PendingAction, lang: Lang) -> str:
    """The exact write, read back to the patient. Built from the pending action, not the model."""
    tz = ZoneInfo(desk.clinic.timezone)
    if pending.kind == "book":
        assert pending.slot is not None
        service = desk.clinic.service(pending.slot.service_id)
        what = service.name.get(lang)
        staff = with_staff(desk.clinic, pending.slot.staff_id, lang)
        at = when(pending.slot.start.astimezone(tz), lang)
        if lang == "es":
            return (
                f"Para confirmar: cita de {what} {staff} el {at} (dura {service.duration_min} "
                "minutos). ¿Está de acuerdo? Responda sí o no."
            )
        return (
            f"Per confirmar: cita de {what} {staff} el {at} (dura {service.duration_min} "
            "minuts). Hi està d'acord? Respongui sí o no."
        )
    assert pending.appointment is not None
    current = pending.appointment
    what = desk.clinic.service(current.service_id).name.get(lang)
    old = when(current.start.astimezone(tz), lang)
    if pending.kind == "move":
        assert pending.slot is not None
        staff = with_staff(desk.clinic, pending.slot.staff_id, lang)
        new = when(pending.slot.start.astimezone(tz), lang)
        if lang == "es":
            return (
                f"Para confirmar: cambio su cita de {what} del {old} al {new}, {staff}. "
                "¿Está de acuerdo? Responda sí o no."
            )
        return (
            f"Per confirmar: canvio la seva cita de {what} del {old} al {new}, {staff}. "
            "Hi està d'acord? Respongui sí o no."
        )
    staff = with_staff(desk.clinic, current.staff_id, lang)
    if lang == "es":
        return (
            f"Para confirmar: anulo su cita de {what} {staff} del {old}. "
            "¿Está de acuerdo? Responda sí o no."
        )
    return (
        f"Per confirmar: anul·lo la seva cita de {what} {staff} del {old}. "
        "Hi està d'acord? Respongui sí o no."
    )


def _describe(desk: Desk, a: Appointment) -> str:
    tz = ZoneInfo(desk.clinic.timezone)
    staff = next(m.name for m in desk.clinic.staff if m.id == a.staff_id)
    return (
        f"{a.id}: {a.service_id} with {staff}, {a.start.astimezone(tz):%a %Y-%m-%d %H:%M}"
        f"-{a.end.astimezone(tz):%H:%M}"
    )


def context_block(desk: Desk, state: State) -> str:
    """The per-turn facts the model needs: today, the next days, who the patient is, and what
    happened to the last proposal. Appended after the fixed prompt (which stays cacheable)."""
    tz = ZoneInfo(desk.clinic.timezone)
    now = desk.clock().astimezone(tz)
    lang = state["lang"]
    days = []
    for i in range(1, 15):
        day = (now + timedelta(days=i)).date()
        hours = desk.clinic.hours_on(day)
        days.append(f"{day.isoformat()} {day_label(day, lang)}{'' if hours else ' (closed)'}")
    lines = [
        "# Context (updated every turn)",
        f"- Now: {when(now, lang)}, {now.year} ({desk.clinic.timezone})."
        f" Today is {now.date().isoformat()}.",
        "- Next days: " + "; ".join(days) + ".",
        f"- The patient is speaking {_LANG_NAME[lang]}: reply in {_LANG_NAME[lang]}.",
    ]
    patient = desk.patients.get(state["patient_id"]) if state["patient_id"] else None
    lines.append(
        f"- Patient: {patient.name} (patient_id {patient.id})."
        if patient
        else "- Patient: not identified yet."
    )
    outcome = state["outcome"]
    if outcome is not None:
        if outcome.result == "done" and outcome.appointment is not None:
            verb = {"book": "Booked", "move": "Moved", "cancel": "Cancelled"}[outcome.kind]
            lines.append(
                f"- Last proposal: the patient confirmed. {verb}"
                f" {_describe(desk, outcome.appointment)}. Tell them it is done."
            )
        elif outcome.result == "declined":
            lines.append(
                "- Last proposal: the patient did NOT confirm it; nothing was written."
                " Read their reply and continue."
            )
        else:
            lines.append(
                f"- Last proposal: confirmed but the write failed ({outcome.problem});"
                " nothing was written. Apologise and offer another option."
            )
    return "\n".join(lines)


# --- graph ---------------------------------------------------------------------------------


def build_graph(desk: Desk) -> CompiledStateGraph[State, None, State, State]:
    async def agent(state: State) -> dict[str, Any]:
        if state["steps"] >= desk.max_steps:
            text = HANDOFF[state["lang"]].format(phone=desk.clinic.phone)
            return {
                "messages": [*state["messages"], AssistantMessage(text, provider="graph")],
                "replies": [*state["replies"], text],
            }
        response = await desk.llm.complete(
            system=f"{desk.prompt}\n\n{context_block(desk, state)}",
            messages=state["messages"],
            tools=SPECS,
            max_tokens=desk.max_tokens,
        )
        message = response.message
        said = message.text.strip()
        return {
            "messages": [*state["messages"], message],
            "replies": [*state["replies"], said] if said else state["replies"],
            "steps": state["steps"] + 1,
        }

    async def tools(state: State) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AssistantMessage)
        current: State = {**state}
        results = []
        for call in last.tool_calls:
            result, updates = await run_tool(desk, current, call)
            results.append(result)
            current = {**current, **updates}  # type: ignore[typeddict-item]
        return {
            "messages": [*state["messages"], ToolResults(results)],
            "patient_id": current["patient_id"],
            "pending": current["pending"],
        }

    async def ask_confirm(state: State) -> dict[str, Any]:
        assert state["pending"] is not None
        text = confirmation_question(desk, state["pending"], state["lang"])
        return {
            "messages": [*state["messages"], AssistantMessage(text, provider="graph")],
            "replies": [*state["replies"], text],
        }

    async def confirm(state: State) -> dict[str, Any]:
        assert state["pending"] is not None
        last = state["messages"][-1]
        label = classify_confirmation(last.content if isinstance(last, UserMessage) else "")
        if label == "yes":
            return {"confirmation": label}
        declined = Outcome(kind=state["pending"].kind, result="declined")
        return {"confirmation": label, "pending": None, "outcome": declined}

    async def act(state: State) -> dict[str, Any]:
        pending = state["pending"]
        assert pending is not None and state["confirmation"] == "yes"
        s = desk.scheduler
        try:
            if pending.kind == "book":
                assert pending.slot is not None
                done = await s.book(pending.patient_id, pending.slot)
            elif pending.kind == "move":
                assert pending.slot is not None and pending.appointment is not None
                done = await s.move(
                    pending.appointment.id, pending.slot, patient_id=pending.patient_id
                )
            else:
                assert pending.appointment is not None
                done = await s.cancel(pending.appointment.id, patient_id=pending.patient_id)
            outcome = Outcome(kind=pending.kind, result="done", appointment=done)
        except SchedulingError as err:
            outcome = Outcome(kind=pending.kind, result="failed", problem=err.problem.value)
        return {"pending": None, "outcome": outcome}

    def route(state: State) -> Literal["confirm", "agent"]:
        return "confirm" if state["pending"] is not None else "agent"

    def after_confirm(state: State) -> Literal["act", "agent"]:
        return "act" if state["confirmation"] == "yes" else "agent"

    def after_agent(state: State) -> Literal["tools", "__end__"]:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AssistantMessage) and last.tool_calls else "__end__"

    def after_tools(state: State) -> Literal["ask_confirm", "agent"]:
        return "ask_confirm" if state["pending"] is not None else "agent"

    graph = StateGraph(State)
    for name, node in (
        ("agent", agent),
        ("tools", tools),
        ("ask_confirm", ask_confirm),
        ("confirm", confirm),
        ("act", act),
    ):
        graph.add_node(name, node)
    graph.add_conditional_edges(START, route)
    graph.add_conditional_edges("confirm", after_confirm)
    graph.add_edge("act", "agent")
    graph.add_conditional_edges("agent", after_agent)
    graph.add_conditional_edges("tools", after_tools)
    graph.add_edge("ask_confirm", "__end__")
    return graph.compile()


class Receptionist:
    """One conversation: feed it the patient's turns, get back what the receptionist says."""

    def __init__(self, desk: Desk, lang: Lang = "es") -> None:
        self.desk = desk
        self.state = new_state(lang)
        self._graph = build_graph(desk)

    async def reply(self, text: str) -> str:
        turn: State = {
            **self.state,
            "messages": [*self.state["messages"], UserMessage(text)],
            "lang": guess_lang(text, default=self.state["lang"]),
            "confirmation": None,
            "replies": [],
            "steps": 0,
        }
        self.state = await self._graph.ainvoke(
            turn, {"recursion_limit": 4 * self.desk.max_steps + 8}
        )  # type: ignore[assignment]
        return "\n".join(self.state["replies"])


def load_prompt(path: Path = PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


def make_desk(llm: LLMClient, clock: Callable[[], datetime], prompt: str | None = None) -> Desk:
    """A desk on the seeded fake calendar: what tests, the CLI and the simulator use."""
    clinic = load_clinic()
    patients, store = load_seed(clinic)
    return Desk(
        clinic=clinic,
        scheduler=Scheduler(clinic, store, patients.get, clock),
        patients=patients,
        llm=llm,
        prompt=prompt if prompt is not None else load_prompt(),
        clock=clock,
    )
