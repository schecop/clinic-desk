"""The booking rules, as pure functions: no I/O, no clock, no storage.

Every calendar adapter (the in-memory fake, Google Calendar) only stores appointments; the rules
that decide whether a slot is bookable live here, once. So the fake and Google cannot disagree,
and `free_slots` cannot offer a slot that `check_slot` would then reject: it is built from it.

A visit must fit inside one opening range; its buffer blocks the staff member after it (and may
run past closing time) but must not overlap anyone else's appointment.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from clinic_desk.clinic import WEEKDAYS, Clinic
from clinic_desk.scheduling.models import Appointment, Slot


class SlotProblem(StrEnum):
    """Why a slot cannot be booked. The values are what the receptionist's tools report."""

    PAST = "past"
    UNKNOWN_SERVICE = "unknown_service"
    SERVICE_NOT_OFFERED = "service_not_offered_by_staff"
    STAFF_OFF = "staff_not_working_that_day"
    OUTSIDE_HOURS = "outside_opening_hours"
    OFF_GRID = "off_the_slot_grid"
    STAFF_BUSY = "staff_busy"
    PATIENT_BUSY = "patient_has_another_appointment"


def visit_times(clinic: Clinic, slot: Slot) -> tuple[datetime, datetime, datetime]:
    """(start, end, block_end) in the clinic's timezone."""
    service = clinic.service(slot.service_id)
    start = slot.start.astimezone(ZoneInfo(clinic.timezone))
    end = start + timedelta(minutes=service.duration_min)
    return start, end, end + timedelta(minutes=service.buffer_min)


def check_slot(
    clinic: Clinic,
    slot: Slot,
    *,
    booked: Iterable[Appointment],
    now: datetime,
    patient_id: str | None = None,
) -> SlotProblem | None:
    """The first rule the slot breaks, or None if it is bookable.

    `booked` are the active appointments around the slot (pass all of them; this filters).
    With `patient_id`, the patient must not be in two places at once either.
    """
    try:
        service = clinic.service(slot.service_id)
    except KeyError:
        return SlotProblem.UNKNOWN_SERVICE
    staff = {m.id: m for m in clinic.staff}.get(slot.staff_id)
    if staff is None or service.id not in staff.services:
        return SlotProblem.SERVICE_NOT_OFFERED
    start, end, block_end = visit_times(clinic, slot)
    if start <= now:
        return SlotProblem.PAST
    if WEEKDAYS[start.weekday()] not in staff.days:
        return SlotProblem.STAFF_OFF
    if start.minute % clinic.slot_minutes or start.second or start.microsecond:
        return SlotProblem.OFF_GRID
    day = start.date()
    if not any(
        start.time() >= open_ and end.time() <= close and end.date() == day
        for open_, close in clinic.hours_on(day)
    ):
        return SlotProblem.OUTSIDE_HOURS
    for other in booked:
        if other.status != "booked":
            continue
        if other.staff_id == slot.staff_id and start < other.block_end and other.start < block_end:
            return SlotProblem.STAFF_BUSY
        if other.patient_id == patient_id and start < other.end and other.start < end:
            return SlotProblem.PATIENT_BUSY
    return None


def free_slots(
    clinic: Clinic,
    service_id: str,
    day_from: date,
    day_to: date,
    *,
    booked: Sequence[Appointment],
    now: datetime,
    staff_ids: Sequence[str] | None = None,
    patient_id: str | None = None,
) -> list[Slot]:
    """Every bookable slot for a service between two dates (inclusive), by time then staff order.

    Candidates are generated on the slot grid inside the opening ranges and kept only if
    `check_slot` accepts them.
    """
    tz = ZoneInfo(clinic.timezone)
    staff = [m for m in clinic.staff_for(service_id) if staff_ids is None or m.id in staff_ids]
    step = timedelta(minutes=clinic.slot_minutes)
    slots: list[Slot] = []
    day = day_from
    while day <= day_to:
        for open_, close in clinic.hours_on(day):
            t = datetime.combine(day, open_, tzinfo=tz)
            range_end = datetime.combine(day, close, tzinfo=tz)
            while t < range_end:
                for member in staff:
                    slot = Slot(service_id=service_id, staff_id=member.id, start=t)
                    if (
                        check_slot(clinic, slot, booked=booked, now=now, patient_id=patient_id)
                        is None
                    ):
                        slots.append(slot)
                t += step
        day += timedelta(days=1)
    return slots
