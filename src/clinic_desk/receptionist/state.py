"""The receptionist's conversation state, and the dependencies its nodes share."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from clinic_desk.clinic import Clinic, Lang
from clinic_desk.llm.base import LLMClient, Message
from clinic_desk.receptionist.language import Confirmation
from clinic_desk.receptionist.translate import LangFix
from clinic_desk.retrieval.base import Retriever
from clinic_desk.scheduling.fake import InMemoryStore, PatientDirectory
from clinic_desk.scheduling.models import Appointment, Slot
from clinic_desk.scheduling.scheduler import Scheduler


class PendingAction(BaseModel):
    """A write the model proposed and the patient has not confirmed yet. Only `act` runs it."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["book", "move", "cancel"]
    patient_id: str
    slot: Slot | None = None  # book, move
    appointment: Appointment | None = None  # move, cancel: as it was when proposed


class Outcome(BaseModel):
    """What happened to the last proposal; shown to the model in its context block."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["book", "move", "cancel"]
    result: Literal["done", "declined", "failed"]
    appointment: Appointment | None = None
    problem: str | None = None


class State(TypedDict):
    messages: list[Message]
    lang: Lang
    patient_id: str | None
    pending: PendingAction | None
    outcome: Outcome | None
    confirmation: Confirmation | None  # how `confirm` read the patient's reply this turn
    replies: list[str]  # what the patient hears this turn
    steps: int  # model calls this turn
    proposed: bool  # a propose_* succeeded in the last tools step
    unclear_streak: int  # replies in a row to a pending proposal that were neither yes nor no
    lang_fix: LangFix | None  # set when the model replied in the wrong language this turn


def new_state(lang: Lang = "es") -> State:
    return State(
        messages=[],
        lang=lang,
        patient_id=None,
        pending=None,
        outcome=None,
        confirmation=None,
        replies=[],
        steps=0,
        proposed=False,
        unclear_streak=0,
        lang_fix=None,
    )


@dataclass
class Desk:
    """Everything the graph's nodes need. One per conversation (the fake store is per run)."""

    clinic: Clinic
    scheduler: Scheduler
    patients: PatientDirectory
    llm: LLMClient
    prompt: str
    clock: Callable[[], datetime]
    max_steps: int = 6  # model calls per patient turn before handing off to a human
    max_tokens: int = 600
    translator: LLMClient | None = None  # language guard model; None: the receptionist's
    # how policies reach the model: all in the prompt, or a search_policies tool (ADR-0003)
    policies: Literal["prompt", "search"] = "prompt"
    retriever: Retriever | None = None  # search mode only; make_desk defaults it to BM25
    store: InMemoryStore | None = None  # the fake calendar, for the simulator's before/after diff
    tool_log: list[ToolEvent] | None = None  # set by the simulator: every tool call, timed


@dataclass(frozen=True)
class ToolEvent:
    """One tool call as it ran: for traces (a tool span) and for the judge (the tool log)."""

    name: str
    arguments: dict[str, Any]
    result: str
    is_error: bool
    start_ns: int
    end_ns: int
