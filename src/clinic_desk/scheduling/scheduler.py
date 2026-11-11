"""The scheduler: the only way to read free slots or write appointments.

It takes the clinic's rules (`availability`), a store (the port) and a clock, and re-checks
every slot at write time under a lock, so two bookings can never take the same staff time even
if both were offered the same free slot. (The Google adapter keeps a check-then-insert race
across processes; one receptionist process per calendar is the stated limit.)

Staff assignment when the patient names nobody: the usual staff member (from the patient's most
recent past visit, if they offer the service), else every staff member who offers it, first
free slot first. Emergencies skip the preference: the first free slot wins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from clinic_desk.clinic import Clinic
from clinic_desk.scheduling.availability import SlotProblem, check_slot, free_slots, visit_times
from clinic_desk.scheduling.models import Appointment, Patient, Slot
from clinic_desk.scheduling.port import AppointmentStore


class BookingProblem(StrEnum):
    UNKNOWN_PATIENT = "unknown_patient"
    REQUIRES_PRIOR_VISIT = "requires_prior_visit"
    NOT_FOUND = "appointment_not_found"
    NOT_ACTIVE = "appointment_cancelled_or_past"
    SERVICE_MISMATCH = "move_must_keep_the_service"


class SchedulingError(Exception):
    def __init__(self, problem: SlotProblem | BookingProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


class Scheduler:
    def __init__(
        self,
        clinic: Clinic,
        store: AppointmentStore,
        patients: Callable[[str], Patient | None],
        clock: Callable[[], datetime],
    ) -> None:
        self.clinic = clinic
        self._store = store
        self._patient = patients
        self._now = clock
        self._lock = asyncio.Lock()

    async def find_slots(
        self,
        service_id: str,
        day_from: date,
        day_to: date,
        *,
        patient_id: str | None = None,
        staff_id: str | None = None,
        limit: int = 6,
    ) -> list[Slot]:
        """Free slots, earliest first, after the staff preference rule (see module docstring)."""
        if patient_id is not None:
            await self._check_patient(patient_id, service_id)
        tz = ZoneInfo(self.clinic.timezone)
        booked = await self._store.between(
            datetime.combine(day_from, time(0), tzinfo=tz),
            datetime.combine(day_to + timedelta(days=2), time(0), tzinfo=tz),
        )

        def search(staff_ids: list[str] | None) -> list[Slot]:
            return free_slots(
                self.clinic,
                service_id,
                day_from,
                day_to,
                booked=booked,
                now=self._now(),
                staff_ids=staff_ids,
                patient_id=patient_id,
            )

        if staff_id is not None:
            return search([staff_id])[:limit]
        usual = await self.usual_staff(patient_id, service_id) if patient_id else None
        if usual is not None and service_id != "emergency":
            preferred = search([usual])
            if preferred:
                return preferred[:limit]
        return search(None)[:limit]

    async def usual_staff(self, patient_id: str, service_id: str) -> str | None:
        offering = {m.id for m in self.clinic.staff_for(service_id)}
        past = [a for a in await self._store.for_patient(patient_id) if self._is_past(a)]
        for appointment in reversed(past):
            if appointment.staff_id in offering:
                return appointment.staff_id
        return None

    async def upcoming(self, patient_id: str) -> list[Appointment]:
        return [
            a
            for a in await self._store.for_patient(patient_id)
            if a.status == "booked" and a.start > self._now()
        ]

    async def book(self, patient_id: str, slot: Slot) -> Appointment:
        async with self._lock:
            await self._check_patient(patient_id, slot.service_id)
            await self._check_free(slot, patient_id, ignore=None)
            start, end, block_end = visit_times(self.clinic, slot)
            draft = Appointment(
                id="",
                patient_id=patient_id,
                service_id=slot.service_id,
                staff_id=slot.staff_id,
                start=start,
                end=end,
                block_end=block_end,
            )
            return await self._store.insert(draft)

    async def move(self, appointment_id: str, slot: Slot) -> Appointment:
        async with self._lock:
            current = await self._active(appointment_id)
            if slot.service_id != current.service_id:
                raise SchedulingError(BookingProblem.SERVICE_MISMATCH)
            await self._check_free(slot, current.patient_id, ignore=current.id)
            start, end, block_end = visit_times(self.clinic, slot)
            moved = current.model_copy(
                update={
                    "staff_id": slot.staff_id,
                    "start": start,
                    "end": end,
                    "block_end": block_end,
                }
            )
            await self._store.update(moved)
            return moved

    async def cancel(self, appointment_id: str) -> Appointment:
        async with self._lock:
            current = await self._active(appointment_id)
            cancelled = current.model_copy(update={"status": "cancelled"})
            await self._store.update(cancelled)
            return cancelled

    async def _check_patient(self, patient_id: str, service_id: str) -> None:
        if self._patient(patient_id) is None:
            raise SchedulingError(BookingProblem.UNKNOWN_PATIENT)
        try:
            service = self.clinic.service(service_id)
        except KeyError:
            raise SchedulingError(SlotProblem.UNKNOWN_SERVICE) from None
        if service.requires_prior_visit and not any(
            self._is_past(a) for a in await self._store.for_patient(patient_id)
        ):
            raise SchedulingError(BookingProblem.REQUIRES_PRIOR_VISIT)

    async def _check_free(self, slot: Slot, patient_id: str, *, ignore: str | None) -> None:
        booked = await self._store.between(
            slot.start - timedelta(days=1), slot.start + timedelta(days=1)
        )
        others = [a for a in booked if a.id != ignore]
        problem = check_slot(
            self.clinic, slot, booked=others, now=self._now(), patient_id=patient_id
        )
        if problem is not None:
            raise SchedulingError(problem)

    async def _active(self, appointment_id: str) -> Appointment:
        current = await self._store.get(appointment_id)
        if current is None:
            raise SchedulingError(BookingProblem.NOT_FOUND)
        if current.status != "booked" or current.start <= self._now():
            raise SchedulingError(BookingProblem.NOT_ACTIVE)
        return current

    def _is_past(self, appointment: Appointment) -> bool:
        return appointment.status == "booked" and appointment.end <= self._now()
