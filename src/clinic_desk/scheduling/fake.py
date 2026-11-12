"""In-memory calendar and patient directory, seeded from `data/clinic/patients.yaml`.

Deterministic on purpose: ids are a counter, so two runs of the same simulation produce the
same appointments and the same traces.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from clinic_desk.clinic import DATA_DIR, Clinic, Lang
from clinic_desk.scheduling.availability import visit_times
from clinic_desk.scheduling.models import Appointment, Patient, Slot, phone_key


class InMemoryStore:
    def __init__(self, appointments: list[Appointment] | None = None) -> None:
        self._by_id: dict[str, Appointment] = {a.id: a for a in appointments or []}
        self._next = 1

    async def between(self, start: datetime, end: datetime) -> list[Appointment]:
        found = sorted(
            (
                a
                for a in self._by_id.values()
                if a.status == "booked" and a.start < end and start < a.block_end
            ),
            key=lambda a: a.start,
        )
        # read now, answer later, like real I/O: without the scheduler's lock, two bookings
        # would both see the slot free (test_double_booking_is_impossible_even_when_concurrent)
        await asyncio.sleep(0)
        return found

    async def for_patient(self, patient_id: str) -> list[Appointment]:
        return sorted(
            (a for a in self._by_id.values() if a.patient_id == patient_id),
            key=lambda a: a.start,
        )

    async def get(self, appointment_id: str) -> Appointment | None:
        return self._by_id.get(appointment_id)

    async def insert(self, appointment: Appointment) -> Appointment:
        while (new_id := f"A{self._next:03d}") in self._by_id:
            self._next += 1
        stored = appointment.model_copy(update={"id": new_id})
        self._by_id[new_id] = stored
        return stored

    async def update(self, appointment: Appointment) -> None:
        if appointment.id not in self._by_id:
            raise KeyError(appointment.id)
        self._by_id[appointment.id] = appointment

    def snapshot(self) -> dict[str, Appointment]:
        """Every appointment, cancelled ones included: the simulator diffs two of these."""
        return dict(self._by_id)


class PatientDirectory:
    """Patients the clinic knows, looked up by phone. New patients are registered on booking."""

    def __init__(self, patients: list[Patient] | None = None) -> None:
        self._by_id: dict[str, Patient] = {p.id: p for p in patients or []}

    def get(self, patient_id: str) -> Patient | None:
        return self._by_id.get(patient_id)

    def find_by_phone(self, phone: str) -> Patient | None:
        key = phone_key(phone)
        return next((p for p in self._by_id.values() if p.phone_key == key), None)

    def register(self, *, name: str, phone: str, lang: Lang) -> Patient:
        existing = self.find_by_phone(phone)
        if existing is not None:
            return existing
        patient = Patient(id=f"n{len(self._by_id) + 1:02d}", name=name, phone=phone, lang=lang)
        self._by_id[patient.id] = patient
        return patient


def load_seed(clinic: Clinic, data_dir: Path = DATA_DIR) -> tuple[PatientDirectory, InMemoryStore]:
    raw: dict[str, Any] = yaml.safe_load((data_dir / "patients.yaml").read_text(encoding="utf-8"))
    patients = [Patient.model_validate(p) for p in raw["patients"]]
    known = {p.id for p in patients}
    tz = ZoneInfo(clinic.timezone)
    appointments = []
    for row in raw["appointments"]:
        if row["patient"] not in known:
            raise ValueError(f"appointment {row['id']}: unknown patient {row['patient']}")
        local = datetime.fromisoformat(row["start"]).replace(tzinfo=tz)
        slot = Slot(service_id=row["service"], staff_id=row["staff"], start=local)
        start, end, block_end = visit_times(clinic, slot)
        appointments.append(
            Appointment(
                id=row["id"],
                patient_id=row["patient"],
                service_id=slot.service_id,
                staff_id=slot.staff_id,
                start=start,
                end=end,
                block_end=block_end,
            )
        )
    return PatientDirectory(patients), InMemoryStore(appointments)
