"""Appointments, slots and patients: the values that cross the calendar port."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from clinic_desk.clinic import Lang


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return value


class Slot(_Value):
    """A bookable start time: one service with one staff member."""

    service_id: str
    staff_id: str
    start: datetime

    _check_start = field_validator("start")(_aware)


class Appointment(_Value):
    id: str
    patient_id: str
    service_id: str
    staff_id: str
    start: datetime
    end: datetime  # start + duration: what the patient is told
    block_end: datetime  # end + buffer: how long the staff member stays blocked
    status: Literal["booked", "cancelled"] = "booked"

    _check_times = field_validator("start", "end", "block_end")(_aware)

    @property
    def slot(self) -> Slot:
        return Slot(service_id=self.service_id, staff_id=self.staff_id, start=self.start)


class Patient(_Value):
    id: str
    name: str
    phone: str
    lang: Lang

    @property
    def phone_key(self) -> str:
        return phone_key(self.phone)


def phone_key(phone: str) -> str:
    """Digits only, without the Spanish prefix, so '600 000 001' matches '+34 600 000 001'."""
    digits = re.sub(r"\D", "", phone)
    return digits[2:] if digits.startswith("34") and len(digits) == 11 else digits
