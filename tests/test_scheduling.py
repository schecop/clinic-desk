"""Booking rules and the scheduler, on the seeded fake calendar (no I/O, fixed clock)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from clinic_desk.clinic import load_clinic
from clinic_desk.scheduling.availability import SlotProblem, check_slot, free_slots
from clinic_desk.scheduling.fake import InMemoryStore, PatientDirectory, load_seed
from clinic_desk.scheduling.models import Appointment, Slot
from clinic_desk.scheduling.scheduler import BookingProblem, Scheduler, SchedulingError

CLINIC = load_clinic()
TZ = ZoneInfo(CLINIC.timezone)
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)  # Wed 11 Nov 2026


def at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=TZ)


def slot(service: str, staff: str, day: str, hhmm: str) -> Slot:
    return Slot(service_id=service, staff_id=staff, start=at(day, hhmm))


def seeded() -> tuple[Scheduler, PatientDirectory, InMemoryStore]:
    patients, store = load_seed(CLINIC)
    return Scheduler(CLINIC, store, patients.get, lambda: NOW), patients, store


def problem(service: str, staff: str, day: str, hhmm: str) -> SlotProblem | None:
    _, _, store = seeded()
    booked = asyncio.run(store.between(at(day, "00:00"), at(day, "23:59")))
    return check_slot(CLINIC, slot(service, staff, day, hhmm), booked=booked, now=NOW)


# --- rules -------------------------------------------------------------------------------------


def test_the_visit_must_fit_the_opening_range_but_the_buffer_may_run_past_closing() -> None:
    # Fri 20 Nov: 09:00-15:00. Extraction is 30 min + 15 min buffer.
    assert problem("extraction", "puig", "2026-11-20", "14:30") is None  # blocked to 15:15
    assert problem("extraction", "puig", "2026-11-20", "14:45") == SlotProblem.OUTSIDE_HOURS
    # Mon 16 Nov: the lunch gap 14:00-16:00
    assert problem("cleaning", "soler", "2026-11-16", "13:30") == SlotProblem.OUTSIDE_HOURS


def test_the_buffer_blocks_the_dentist_after_the_visit() -> None:
    # A001: extraction with Puig on Mon 16 Nov 12:00, visit to 12:30, blocked to 12:45
    assert problem("checkup", "puig", "2026-11-16", "12:30") == SlotProblem.STAFF_BUSY
    assert problem("checkup", "puig", "2026-11-16", "12:45") is None
    # a visit whose own buffer would run into A001 is refused too
    assert problem("extraction", "puig", "2026-11-16", "11:30") == SlotProblem.STAFF_BUSY
    assert problem("checkup", "roca", "2026-11-16", "12:00") is None  # another dentist is free


@pytest.mark.parametrize(
    ("service", "staff", "day", "hhmm", "expected"),
    [
        ("checkup", "roca", "2026-11-17", "10:00", SlotProblem.STAFF_OFF),  # Roca: Mon/Wed/Fri
        ("root_canal", "puig", "2026-11-16", "10:00", SlotProblem.SERVICE_NOT_OFFERED),
        ("checkup", "puig", "2026-11-11", "09:45", SlotProblem.PAST),
        ("checkup", "puig", "2026-11-16", "10:05", SlotProblem.OFF_GRID),
        ("checkup", "puig", "2026-12-08", "10:00", SlotProblem.OUTSIDE_HOURS),  # holiday
        ("checkup", "puig", "2026-12-24", "16:00", SlotProblem.OUTSIDE_HOURS),  # special hours
        ("checkup", "puig", "2026-11-14", "10:00", SlotProblem.STAFF_OFF),  # Saturday
        ("nope", "puig", "2026-11-16", "10:00", SlotProblem.UNKNOWN_SERVICE),
    ],
)
def test_check_slot_names_the_broken_rule(
    service: str, staff: str, day: str, hhmm: str, expected: SlotProblem
) -> None:
    assert problem(service, staff, day, hhmm) == expected


def test_free_slots_only_offers_what_check_slot_accepts_and_never_overlaps() -> None:
    _, _, store = seeded()
    booked = asyncio.run(store.between(at("2026-11-11", "00:00"), at("2026-11-21", "00:00")))
    for service in ("extraction", "root_canal", "cleaning", "emergency"):
        slots = free_slots(
            CLINIC, service, date(2026, 11, 11), date(2026, 11, 20), booked=booked, now=NOW
        )
        assert slots, service
        assert slots == sorted(slots, key=lambda s: s.start)
        for s in slots:
            assert check_slot(CLINIC, s, booked=booked, now=NOW) is None
            assert s.start > NOW


def test_special_hours_and_holidays_shape_the_offer() -> None:
    eve = free_slots(CLINIC, "checkup", date(2026, 12, 24), date(2026, 12, 24), booked=[], now=NOW)
    assert eve and max(s.start for s in eve).hour < 14
    assert (
        free_slots(CLINIC, "checkup", date(2026, 12, 8), date(2026, 12, 8), booked=[], now=NOW)
        == []
    )


def test_every_seeded_upcoming_appointment_is_itself_valid() -> None:
    _, _, store = seeded()
    upcoming = [a for a in asyncio.run(store.between(NOW, NOW + timedelta(days=60)))]
    assert len(upcoming) == 5
    for a in upcoming:
        others = [o for o in upcoming if o.id != a.id]
        assert check_slot(CLINIC, a.slot, booked=others, now=NOW, patient_id=a.patient_id) is None


# --- scheduler ---------------------------------------------------------------------------------


def test_double_booking_is_impossible_even_when_concurrent() -> None:
    scheduler, _, _ = seeded()
    target = slot("checkup", "puig", "2026-11-18", "10:00")

    async def race() -> list[Appointment | BaseException]:
        return list(
            await asyncio.gather(
                scheduler.book("p01", target), scheduler.book("p02", target), return_exceptions=True
            )
        )

    results = asyncio.run(race())
    errors = [r for r in results if isinstance(r, SchedulingError)]
    assert len(errors) == 1 and errors[0].problem == SlotProblem.STAFF_BUSY


def test_a_new_patient_can_only_book_what_needs_no_prior_visit() -> None:
    scheduler, patients, _ = seeded()
    new = patients.register(name="Lucía Prova", phone="611 000 999", lang="es")
    with pytest.raises(SchedulingError) as err:
        asyncio.run(scheduler.book(new.id, slot("checkup", "puig", "2026-11-18", "10:00")))
    assert err.value.problem == BookingProblem.REQUIRES_PRIOR_VISIT
    booked = asyncio.run(scheduler.book(new.id, slot("first_visit", "puig", "2026-11-18", "10:00")))
    assert booked.id == "A006"  # ids continue after the seed
    assert booked.end - booked.start == timedelta(minutes=30)


def test_unknown_patients_are_refused() -> None:
    scheduler, _, _ = seeded()
    with pytest.raises(SchedulingError) as err:
        asyncio.run(scheduler.book("ghost", slot("first_visit", "puig", "2026-11-18", "10:00")))
    assert err.value.problem == BookingProblem.UNKNOWN_PATIENT


def test_a_patient_cannot_be_in_two_places_at_once() -> None:
    scheduler, _, _ = seeded()
    # p01 has a cleaning with Soler on Tue 17 Nov 10:00-10:45
    with pytest.raises(SchedulingError) as err:
        asyncio.run(scheduler.book("p01", slot("checkup", "puig", "2026-11-17", "10:30")))
    assert err.value.problem == SlotProblem.PATIENT_BUSY


def test_assignment_prefers_the_usual_dentist_then_anyone() -> None:
    scheduler, _, _ = seeded()
    week = (date(2026, 11, 16), date(2026, 11, 20))
    # Carmen (p02) always saw Roca
    assert {
        s.staff_id for s in asyncio.run(scheduler.find_slots("checkup", *week, patient_id="p02"))
    } == {"roca"}
    # ...unless she asks for someone else
    asked = asyncio.run(scheduler.find_slots("checkup", *week, patient_id="p02", staff_id="puig"))
    assert {s.staff_id for s in asked} == {"puig"}
    # no usual dentist: the first free slot, staff in file order (Puig before Roca)
    anyone = asyncio.run(scheduler.find_slots("checkup", *week, patient_id="p06"))
    assert anyone[0].staff_id == "puig" and anyone[0].start == at("2026-11-16", "09:00")
    # emergencies ignore the preference
    urgent = asyncio.run(scheduler.find_slots("emergency", *week, patient_id="p02"))
    assert urgent[0].start == at("2026-11-16", "09:00") and urgent[0].staff_id == "puig"


def test_move_frees_the_old_slot_and_a_failed_move_changes_nothing() -> None:
    scheduler, _, store = seeded()
    old = slot("checkup", "roca", "2026-11-18", "09:30")  # A003, Carmen
    moved = asyncio.run(scheduler.move("A003", slot("checkup", "roca", "2026-11-18", "11:00")))
    assert moved.id == "A003" and moved.start == at("2026-11-18", "11:00")
    assert asyncio.run(scheduler.book("p01", old)).start == old.start  # old slot is free again

    with pytest.raises(SchedulingError) as err:  # onto the slot p01 just took
        asyncio.run(scheduler.move("A003", old))
    assert err.value.problem == SlotProblem.STAFF_BUSY
    assert asyncio.run(store.get("A003")) == moved

    with pytest.raises(SchedulingError) as err:
        asyncio.run(scheduler.move("A003", slot("filling", "roca", "2026-11-18", "12:00")))
    assert err.value.problem == BookingProblem.SERVICE_MISMATCH


def test_cancel_frees_the_slot_and_only_works_once() -> None:
    scheduler, _, _ = seeded()
    cancelled = asyncio.run(scheduler.cancel("A005"))
    assert cancelled.status == "cancelled"
    assert asyncio.run(scheduler.upcoming("p06")) == []
    asyncio.run(scheduler.book("p01", slot("whitening", "soler", "2026-11-20", "09:00")))
    for appointment_id, expected in (
        ("A005", BookingProblem.NOT_ACTIVE),
        ("H001", BookingProblem.NOT_ACTIVE),
        ("Z9", BookingProblem.NOT_FOUND),
    ):
        with pytest.raises(SchedulingError) as err:
            asyncio.run(scheduler.cancel(appointment_id))
        assert err.value.problem == expected


def test_patients_are_found_by_phone_in_any_format() -> None:
    _, patients, _ = seeded()
    for phone in ("+34 600 000 001", "600000001", "34-600-000-001", "600 00 00 01"):
        found = patients.find_by_phone(phone)
        assert found is not None and found.id == "p01", phone
    assert patients.register(name="Otro", phone="600000001", lang="es").id == "p01"


def test_the_time_window_applies_before_the_usual_dentist_preference() -> None:
    scheduler, _, _ = seeded()
    fri = (date(2026, 11, 20), date(2026, 11, 20))  # Friday: mornings only
    # Carmen's usual dentist is Roca; a window with nothing open returns nothing
    assert (
        asyncio.run(scheduler.find_slots("checkup", *fri, patient_id="p02", time_from=time(16)))
        == []
    )
    late = asyncio.run(
        scheduler.find_slots("checkup", *fri, patient_id="p02", time_from=time(14), limit=None)
    )
    assert late and all(s.start.time() >= time(14) for s in late)
    assert {s.staff_id for s in late} == {"roca"}
