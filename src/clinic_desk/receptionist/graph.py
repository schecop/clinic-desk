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

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from clinic_desk.clinic import Clinic, Lang, load_clinic
from clinic_desk.llm.base import AssistantMessage, LLMClient, ToolResults, UserMessage
from clinic_desk.receptionist.claims import claims_write
from clinic_desk.receptionist.language import (
    classify_confirmation,
    day_label,
    guess_lang,
    of,
    when,
    with_staff,
)
from clinic_desk.receptionist.state import (
    Desk,
    Outcome,
    PendingAction,
    State,
    ToolEvent,
    new_state,
)
from clinic_desk.receptionist.tools import run_tool, specs_for
from clinic_desk.receptionist.translate import LangFix, needs_translation, translate
from clinic_desk.retrieval import BM25, Retriever, chunks_from
from clinic_desk.scheduling.fake import load_seed
from clinic_desk.scheduling.models import Appointment
from clinic_desk.scheduling.scheduler import Scheduler, SchedulingError

PROMPT_PATH = Path("prompts/receptionist.md")
MAX_UNCLEAR = 3  # replies in a row that are neither yes nor no before a proposal is dropped

HANDOFF: dict[Lang, str] = {
    "es": "Disculpe, no he podido resolverlo. Le paso con la recepción: llame al {phone}.",
    "ca": "Disculpi, no ho he pogut resoldre. Li passo amb la recepció: truqui al {phone}.",
}
# spoken instead of a reply that claimed a write when nothing was written or proposed
NOT_DONE: dict[Lang, str] = {
    "es": "Perdone, me he expresado mal: todavía no he hecho ningún cambio en su cita."
    " ¿Qué quiere que haga?",
    "ca": "Perdoni, m'he explicat malament: encara no he fet cap canvi a la seva cita."
    " Què vol que faci?",
}
_LANG_NAME: dict[Lang, str] = {"es": "Spanish", "ca": "Catalan"}


# --- rendering -----------------------------------------------------------------------------


def confirmation_question(desk: Desk, pending: PendingAction, lang: Lang) -> str:
    """The exact write, read back to the patient. Built from the pending action, not the model."""
    tz = ZoneInfo(desk.clinic.timezone)
    if pending.kind == "book":
        assert pending.slot is not None
        service = desk.clinic.service(pending.slot.service_id)
        what = of(service.name.get(lang), lang)
        staff = with_staff(desk.clinic, pending.slot.staff_id, lang)
        at = when(pending.slot.start.astimezone(tz), lang)
        if lang == "es":
            return (
                f"Para confirmar: cita {what} {staff} el {at} (dura {service.duration_min} "
                "minutos). ¿Está de acuerdo? Responda sí o no."
            )
        return (
            f"Per confirmar: cita {what} {staff} el {at} (dura {service.duration_min} "
            "minuts). Hi està d'acord? Respongui sí o no."
        )
    assert pending.appointment is not None
    current = pending.appointment
    what = of(desk.clinic.service(current.service_id).name.get(lang), lang)
    old = when(current.start.astimezone(tz), lang)
    if pending.kind == "move":
        assert pending.slot is not None
        staff = with_staff(desk.clinic, pending.slot.staff_id, lang)
        new = when(pending.slot.start.astimezone(tz), lang)
        if lang == "es":
            return (
                f"Para confirmar: cambio su cita {what} del {old} al {new}, {staff}. "
                "¿Está de acuerdo? Responda sí o no."
            )
        return (
            f"Per confirmar: canvio la seva cita {what} del {old} al {new}, {staff}. "
            "Hi està d'acord? Respongui sí o no."
        )
    staff = with_staff(desk.clinic, current.staff_id, lang)
    if lang == "es":
        return (
            f"Para confirmar: anulo su cita {what} {staff} del {old}. "
            "¿Está de acuerdo? Responda sí o no."
        )
    return (
        f"Per confirmar: anul·lo la seva cita {what} {staff} del {old}. "
        "Hi està d'acord? Respongui sí o no."
    )


def policies_block(clinic: Clinic) -> str:
    """Every policy in both languages, for the fixed part of the prompt (policies mode "prompt").

    Built from the clinic's data, not written in `prompts/`: these are facts, and the coach edits
    only the receptionist's instructions. Both languages, so a language switch does not change
    the prompt and break its cache.
    """
    lines = [
        "# Clinic policies",
        "The clinic's own policies, in Spanish and Catalan. Answer policy questions only from"
        " here, in the patient's language, and say only what they state.",
    ]
    for p in clinic.policies:
        lines += ["", f"## {p.id}"]
        lines += [f"- {lang}: {p.title.get(lang)}. {p.body.get(lang)}" for lang in clinic.languages]
    return "\n".join(lines)


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
    ]
    patient = desk.patients.get(state["patient_id"]) if state["patient_id"] else None
    lines.append(
        f"- Patient: {patient.name} (patient_id {patient.id})."
        if patient
        else "- Patient: not identified yet."
    )
    pending = state["pending"]
    if pending is not None:
        question = confirmation_question(desk, pending, lang)
        lines.append(
            f'- Pending proposal, NOT confirmed and NOT written: "{question}" The patient'
            " answered with neither a yes nor a no. Answer what they said in one or two"
            " sentences. If they want a different slot, call propose_* again (it replaces this"
            " one). Do not ask for confirmation yourself: the system repeats the question after"
            " your reply. Never say it is done."
        )
    outcome = state["outcome"]
    if outcome is not None and pending is None:
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
    # last on purpose (the most recent thing the model reads): in the first real chat the
    # model answered a Catalan question in Spanish, pulled by earlier Spanish turns (FAILURES)
    name = _LANG_NAME[lang]
    lines.append(
        f"- Reply language: {name}. The patient's latest message is in {name}: write your whole"
        f" reply in {name}, even if earlier turns, tool results or policies are in another"
        " language."
    )
    return "\n".join(lines)


# --- graph ---------------------------------------------------------------------------------


def _may_claim(state: State) -> bool:
    """A reply may say "done" only when nothing is pending and a write has been made in this
    call (the context block tells the model which one)."""
    outcome = state["outcome"]
    return state["pending"] is None and outcome is not None and outcome.result == "done"


def build_graph(desk: Desk) -> CompiledStateGraph[State, None, State, State]:
    # the fixed part first, identical on every call (cacheable); the per-turn context after it
    fixed_prompt = desk.prompt
    if desk.policies == "prompt":
        fixed_prompt += "\n\n" + policies_block(desk.clinic)
    specs = specs_for(desk)

    async def agent(state: State) -> dict[str, Any]:
        if state["steps"] >= desk.max_steps:
            text = HANDOFF[state["lang"]].format(phone=desk.clinic.phone)
            return {
                "messages": [*state["messages"], AssistantMessage(text, provider="graph")],
                "replies": [*state["replies"], text],
            }
        response = await desk.llm.complete(
            system=f"{fixed_prompt}\n\n{context_block(desk, state)}",
            messages=state["messages"],
            tools=specs,
            max_tokens=desk.max_tokens,
        )
        message = response.message
        # only a final reply is spoken: text next to a tool call is a preamble ("voy a buscar
        # su ficha") that, joined to the answer, made the patient hear two greetings
        said = "" if message.tool_calls else message.text.strip()
        fix: LangFix | None = state["lang_fix"]
        if said and needs_translation(said, state["lang"]):
            translated = await translate(
                desk.translator or desk.llm, said, state["lang"], desk.max_tokens
            )
            fix = "translated" if translated else "kept"
            if translated:
                said = translated
                # the history holds what the patient heard; raw is dropped so the provider
                # is not replayed the untranslated turn
                message = AssistantMessage(translated, provider=message.provider)
        blocked = bool(said) and claims_write(said) and not _may_claim(state)
        if blocked:
            # the gate held but the words did not: "queda anul·lada" while the cancel was still
            # waiting for a yes (FAILURES, Aina; Carmen in D2). Not spoken, not kept in the
            # history. With a proposal pending, ask_confirm repeats the question; otherwise a
            # fixed line takes it back.
            if state["pending"] is not None:
                return {"steps": state["steps"] + 1, "lang_fix": fix, "claim_blocked": True}
            said = NOT_DONE[state["lang"]]
            message = AssistantMessage(said, provider="graph")
        return {
            "messages": [*state["messages"], message],
            "replies": [*state["replies"], said] if said else state["replies"],
            "steps": state["steps"] + 1,
            "lang_fix": fix,
            "claim_blocked": state["claim_blocked"] or blocked,
        }

    async def tools(state: State) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AssistantMessage)
        current: State = {**state}
        before = state["pending"]
        results = []
        for call in last.tool_calls:
            start = time.time_ns()
            result, updates = await run_tool(desk, current, call)
            if desk.tool_log is not None:
                desk.tool_log.append(
                    ToolEvent(
                        call.name,
                        call.arguments,
                        result.content,
                        result.is_error,
                        start,
                        time.time_ns(),
                    )
                )
            results.append(result)
            current = {**current, **updates}  # type: ignore[typeddict-item]
        return {
            "messages": [*state["messages"], ToolResults(results)],
            "patient_id": current["patient_id"],
            "pending": current["pending"],
            "proposed": (
                fresh := current["pending"] is not None and current["pending"] is not before
            ),
            "unclear_streak": 0 if fresh else state["unclear_streak"],
        }

    async def ask_confirm(state: State) -> dict[str, Any]:
        assert state["pending"] is not None
        text = confirmation_question(desk, state["pending"], state["lang"])
        # the model often copies last turn's question into its answer; the patient must hear
        # it once (calib3: twice in a row, aina-late-cancel r0 and marc r0)
        said = [r.replace(text, "").strip() for r in state["replies"]]
        return {
            "messages": [*state["messages"], AssistantMessage(text, provider="graph")],
            "replies": [*(r for r in said if r), text],
        }

    async def confirm(state: State) -> dict[str, Any]:
        assert state["pending"] is not None
        last = state["messages"][-1]
        label = classify_confirmation(last.content if isinstance(last, UserMessage) else "")
        if label == "yes":
            return {"confirmation": label, "unclear_streak": 0}
        streak = state["unclear_streak"] + 1
        if label == "unclear" and streak < MAX_UNCLEAR:
            # keep the proposal: the agent answers what they said, then ask_confirm repeats
            # the question. Dropping it here let the model ask "¿confirma?" itself and then
            # claim a move that never happened (FAILURES)
            return {"confirmation": label, "unclear_streak": streak}
        declined = Outcome(kind=state["pending"].kind, result="declined")
        return {"confirmation": label, "pending": None, "outcome": declined, "unclear_streak": 0}

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

    def after_agent(state: State) -> Literal["tools", "ask_confirm", "__end__"]:
        last = state["messages"][-1]
        if isinstance(last, AssistantMessage) and last.tool_calls:
            return "tools"
        # a proposal still pending after the agent's answer: the graph asks again, verbatim
        return "ask_confirm" if state["pending"] is not None else "__end__"

    def after_tools(state: State) -> Literal["ask_confirm", "agent"]:
        return "ask_confirm" if state["proposed"] else "agent"

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
            "proposed": False,
            "lang_fix": None,
            "claim_blocked": False,
        }
        self.state = await self._graph.ainvoke(
            turn, {"recursion_limit": 4 * self.desk.max_steps + 8}
        )  # type: ignore[assignment]
        return "\n".join(self.state["replies"])


def load_prompt(path: Path = PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


def make_desk(
    llm: LLMClient,
    clock: Callable[[], datetime],
    prompt: str | None = None,
    translator: LLMClient | None = None,
    policies: Literal["prompt", "search"] = "prompt",
    retriever: Retriever | None = None,
) -> Desk:
    """A desk on the seeded fake calendar: what tests, the CLI and the simulator use.

    In search mode without a retriever, BM25 (local, free): tests never call an embedding API.
    """
    clinic = load_clinic()
    if policies == "search" and retriever is None:
        retriever = BM25(chunks_from(clinic))
    patients, store = load_seed(clinic)
    return Desk(
        clinic=clinic,
        scheduler=Scheduler(clinic, store, patients.get, clock),
        patients=patients,
        llm=llm,
        prompt=prompt if prompt is not None else load_prompt(),
        clock=clock,
        translator=translator,
        policies=policies,
        retriever=retriever,
        store=store,
    )
