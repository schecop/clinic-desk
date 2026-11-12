"""Persona cards: who the simulated patient is, what they want, and what success looks like.

One YAML file per persona in `data/personas/`. `expect` is checked by code against the fake
calendar; `judge` lists only what code cannot check. The visible / held-out split lives in
`data/personas/split.yaml`, made once (stratified by category, fixed seed) and never edited:
the coach only ever sees the visible personas.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from clinic_desk.clinic import WEEKDAYS, Clinic, Lang

PERSONAS_DIR = Path("data/personas")
SPLIT_SEED = 20261112

Category = Literal["book", "move", "cancel", "policy", "medical", "language", "edge"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Caller(_Strict):
    """Who calls: a seeded patient (by id) or a new one (name and phone, not in the seed)."""

    id: str | None = None  # seeded patient id, e.g. p02
    name: str
    phone: str


class LanguagePlan(_Strict):
    start: Lang
    switch_to: Lang | None = None
    at_turn: int | None = Field(default=None, ge=2)  # the patient's turn number, from 1
    mixes: bool = False  # code-switches inside turns; the language check is skipped

    def expected(self, turn: int) -> Lang:
        if self.switch_to and self.at_turn and turn >= self.at_turn:
            return self.switch_to
        return self.start


class SlotMatch(_Strict):
    """What a booked or moved appointment must look like. Unset fields match anything."""

    service: str
    weekday: str | None = None
    after: str | None = None  # HH:MM local, start >= after
    before: str | None = None  # HH:MM local, start < before
    staff: str | None = None
    date: str | None = None  # YYYY-MM-DD local


class CalendarExpect(_Strict):
    """Exactly one of these. `unchanged`: no write at all. `any`: not checked (e.g. 112 case)."""

    booked: SlotMatch | None = None
    moved: SlotMatch | None = None  # the patient's appointment of that service, at a new time
    cancelled: str | None = None  # the appointment id
    unchanged: bool = False
    any: bool = False

    @model_validator(mode="after")
    def _one(self) -> CalendarExpect:
        set_ = [self.booked, self.moved, self.cancelled, self.unchanged or None, self.any or None]
        if sum(x is not None for x in set_) != 1:
            raise ValueError("expect.calendar needs exactly one of booked/moved/cancelled/...")
        return self


class Expect(_Strict):
    calendar: CalendarExpect


class Persona(_Strict):
    id: str
    category: Category
    caller: Caller
    goal: str
    traits: list[str] = []
    hidden: str | None = None  # held back until the condition the text gives
    language: LanguagePlan
    max_turns: int = Field(default=12, ge=2, le=20)
    now: datetime | None = None  # local time of the call; default: the simulator's clock
    expect: Expect
    judge: list[str] = []


def load_personas(clinic: Clinic, directory: Path = PERSONAS_DIR) -> list[Persona]:
    personas = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "split.yaml":
            continue
        persona = Persona.model_validate(yaml.safe_load(path.read_text("utf-8")))
        if persona.id != path.stem:
            raise ValueError(f"{path.name}: id {persona.id!r} does not match the file name")
        _cross_check(persona, clinic)
        personas.append(persona)
    return personas


def _cross_check(p: Persona, clinic: Clinic) -> None:
    services = {s.id for s in clinic.services}
    staff = {m.id for m in clinic.staff}
    for match in (p.expect.calendar.booked, p.expect.calendar.moved):
        if match is None:
            continue
        if match.service not in services:
            raise ValueError(f"{p.id}: unknown service {match.service!r}")
        if match.staff and match.staff not in staff:
            raise ValueError(f"{p.id}: unknown staff {match.staff!r}")
        if match.weekday and match.weekday not in WEEKDAYS:
            raise ValueError(f"{p.id}: unknown weekday {match.weekday!r}")


def make_split(personas: list[Persona], seed: int = SPLIT_SEED) -> dict[str, list[str]]:
    """One held-out persona per category that has 2 or more, chosen with a fixed seed."""
    rng = random.Random(seed)
    by_category: dict[str, list[str]] = defaultdict(list)
    for p in personas:
        by_category[p.category].append(p.id)
    held_out = sorted(
        rng.choice(sorted(ids)) for _, ids in sorted(by_category.items()) if len(ids) >= 2
    )
    visible = sorted(p.id for p in personas if p.id not in held_out)
    return {"visible": visible, "held_out": held_out}


def load_split(directory: Path = PERSONAS_DIR) -> dict[str, list[str]]:
    split: dict[str, list[str]] = yaml.safe_load((directory / "split.yaml").read_text("utf-8"))
    return split
