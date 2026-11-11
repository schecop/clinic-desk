"""The calendar port: storage only. Booking rules live in `availability`, enforced by `Scheduler`.

Adapters: `InMemoryStore` (every simulation and test) and, on D6, Google Calendar (demo and the
OAuth pass). An adapter never decides whether a slot is free; it stores what the scheduler wrote.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from clinic_desk.scheduling.models import Appointment


class AppointmentStore(Protocol):
    async def between(self, start: datetime, end: datetime) -> list[Appointment]:
        """Booked appointments whose blocked time (start to block_end) overlaps [start, end)."""
        ...

    async def for_patient(self, patient_id: str) -> list[Appointment]:
        """All of a patient's appointments, any status, oldest first."""
        ...

    async def get(self, appointment_id: str) -> Appointment | None: ...

    async def insert(self, appointment: Appointment) -> Appointment:
        """Store a new appointment; the store assigns the id (the one passed in is ignored)."""
        ...

    async def update(self, appointment: Appointment) -> None: ...
