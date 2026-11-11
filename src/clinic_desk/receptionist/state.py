"""The receptionist's conversation state, and the dependencies its nodes share."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from clinic_desk.clinic import Clinic, Lang
from clinic_desk.llm.base import LLMClient, Message
from clinic_desk.receptionist.language import Confirmation
from clinic_desk.scheduling.fake import PatientDirectory
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
