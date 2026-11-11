"""The clinic's facts (hours, staff, services, policies), loaded from `data/clinic/` and validated.

Everything the receptionist states as a fact comes from here through a tool, never from the
model. Validation is strict and cross-checked at load time, so a broken fixture fails before any
simulation runs, not halfway through a pass of hundreds of conversations.
"""

from __future__ import annotations

from datetime import date, time
from itertools import pairwise
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Lang = Literal["es", "ca"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DATA_DIR = Path("data/clinic")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Text(_Strict):
    """The same text in every supported language: a missing translation fails at load."""

    es: str = Field(min_length=1)
    ca: str = Field(min_length=1)

    def get(self, lang: Lang) -> str:
        return self.es if lang == "es" else self.ca


class Staff(_Strict):
    id: str
    name: str
    role: Text
    days: list[Weekday] = Field(min_length=1)
    services: list[str] = Field(min_length=1)


class Service(_Strict):
    id: str
    name: Text
    duration_min: int = Field(gt=0)
    price_eur: int = Field(ge=0)
    requires_prior_visit: bool = False
    # minutes the staff member stays blocked after the visit, in case it runs over; the patient
    # is told only `duration_min`. Fixed per service for now (see PLAN: buffer analysis).
    buffer_min: int = Field(default=0, ge=0)
    notes: Text | None = None


class Policy(_Strict):
    id: str
    title: Text
    body: Text


class Clinic(_Strict):
    name: str
    address: str
    phone: str
    timezone: str
    languages: list[Lang]
    slot_minutes: int = Field(gt=0)
    opening_hours: dict[Weekday, list[tuple[time, time]]]
    closed_dates: list[date] = []
    # dates with reduced hours (e.g. Christmas Eve): they replace that weekday's ranges
    special_hours: dict[date, list[tuple[time, time]]] = {}
    staff: list[Staff]
    services: list[Service]
    policies: list[Policy] = []

    @model_validator(mode="after")
    def _cross_checks(self) -> Clinic:
        for day, ranges in self.opening_hours.items():
            self._check_ranges(str(day), ranges)
        for special_day, ranges in self.special_hours.items():
            self._check_ranges(special_day.isoformat(), ranges)
            if special_day in self.closed_dates:
                raise ValueError(f"{special_day} is both closed and has special hours")
        _unique("staff", [s.id for s in self.staff])
        _unique("service", [s.id for s in self.services])
        _unique("policy", [p.id for p in self.policies])
        service_ids = {s.id for s in self.services}
        for member in self.staff:
            unknown = set(member.services) - service_ids
            if unknown:
                raise ValueError(f"staff {member.id} offers unknown services: {sorted(unknown)}")
            closed = [d for d in member.days if d not in self.opening_hours]
            if closed:
                raise ValueError(f"staff {member.id} works on closed days: {closed}")
        offered = {sid for member in self.staff for sid in member.services}
        orphans = service_ids - offered
        if orphans:
            raise ValueError(f"services nobody offers: {sorted(orphans)}")
        for service in self.services:
            if service.duration_min % self.slot_minutes:
                raise ValueError(f"service {service.id}: duration off the slot grid")
            if service.buffer_min % self.slot_minutes:
                raise ValueError(f"service {service.id}: buffer off the slot grid")
        return self

    def _check_ranges(self, label: str, ranges: list[tuple[time, time]]) -> None:
        for start, end in ranges:
            if start >= end:
                raise ValueError(f"{label}: range {start}-{end} is empty or reversed")
            if start.minute % self.slot_minutes or end.minute % self.slot_minutes:
                raise ValueError(f"{label}: {start}-{end} is off the {self.slot_minutes}-min grid")
        for (_, prev_end), (next_start, _) in pairwise(ranges):
            if next_start < prev_end:
                raise ValueError(f"{label}: opening ranges overlap")

    def hours_on(self, day: date) -> list[tuple[time, time]]:
        """Opening ranges on a date: closed dates, then special hours, then the weekday."""
        if day in self.closed_dates:
            return []
        if day in self.special_hours:
            return self.special_hours[day]
        return self.opening_hours.get(WEEKDAYS[day.weekday()], [])

    def service(self, service_id: str) -> Service:
        for s in self.services:
            if s.id == service_id:
                return s
        raise KeyError(service_id)

    def staff_for(self, service_id: str) -> list[Staff]:
        return [m for m in self.staff if service_id in m.services]


def _unique(kind: str, ids: list[str]) -> None:
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate {kind} ids: {dupes}")


def load_clinic(data_dir: Path = DATA_DIR) -> Clinic:
    raw = yaml.safe_load((data_dir / "clinic.yaml").read_text(encoding="utf-8"))
    policies_path = data_dir / "policies.yaml"
    if policies_path.exists():
        raw["policies"] = yaml.safe_load(policies_path.read_text(encoding="utf-8"))
    return Clinic.model_validate(raw)
