"""The receptionist's tools. None of them writes to the calendar.

Read tools answer from the clinic data and the scheduler (facts come from tools, never from the
model). The `propose_*` tools validate a write with the scheduler's dry run and park it in the
state as `pending`; the graph then asks the patient to confirm, and only the `act` node writes.

Guards live here, not in the prompt: no slot search or proposal before the patient is
identified, and a patient can only move or cancel their own appointments.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from clinic_desk.clinic import WEEKDAYS, Lang
from clinic_desk.llm.base import ToolCall, ToolResult, ToolSpec
from clinic_desk.receptionist.language import WEEKDAY_NAMES, weekday_name
from clinic_desk.receptionist.state import Desk, PendingAction, State
from clinic_desk.scheduling.models import Appointment, Slot
from clinic_desk.scheduling.scheduler import SchedulingError

MAX_SEARCH_DAYS = 14
MAX_SLOTS_SHOWN = 6


class ToolError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdentifyPatient(_Args):
    phone: str = Field(description="The patient's phone number, any format")


class RegisterPatient(_Args):
    name: str = Field(min_length=2, description="Full name, as the patient said it")
    phone: str = Field(min_length=9)


class NoArgs(_Args):
    pass


class SearchPolicies(_Args):
    query: str = Field(min_length=2, description="The patient's question, in their words")


class FindSlots(_Args):
    service_id: str
    date_from: date = Field(description="YYYY-MM-DD, first day to search")
    date_to: date = Field(description=f"YYYY-MM-DD, last day (at most {MAX_SEARCH_DAYS} days)")
    staff_id: str | None = Field(default=None, description="Only if the patient asks for someone")
    time_from: time | None = Field(
        default=None, description="HH:MM, earliest start; e.g. 16:00 for afternoons"
    )
    time_to: time | None = Field(default=None, description="HH:MM, latest start (exclusive)")


class ProposeBooking(_Args):
    service_id: str
    staff_id: str
    start: datetime = Field(description="Local start time from find_slots, YYYY-MM-DDTHH:MM")


class ProposeMove(_Args):
    appointment_id: str
    staff_id: str
    start: datetime = Field(description="New local start time from find_slots")


class ProposeCancel(_Args):
    appointment_id: str


Handler = Callable[[Desk, State, Any], Awaitable[tuple[dict[str, Any], dict[str, Any]]]]


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    args: type[_Args]
    handler: Handler


# --- helpers -------------------------------------------------------------------------------


def _local(desk: Desk, moment: datetime) -> datetime:
    tz = ZoneInfo(desk.clinic.timezone)
    return moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)


def _patient(state: State) -> str:
    if state["patient_id"] is None:
        raise ToolError(
            "identify_patient_first", "Ask for the phone number and call identify_patient."
        )
    return state["patient_id"]


def _staff_name(desk: Desk, staff_id: str) -> str:
    return next((m.name for m in desk.clinic.staff if m.id == staff_id), staff_id)


def _slot_view(desk: Desk, slot: Slot, lang: Lang) -> dict[str, Any]:
    start = _local(desk, slot.start)
    return {
        "service_id": slot.service_id,
        "staff_id": slot.staff_id,
        "staff_name": _staff_name(desk, slot.staff_id),
        "start": f"{start:%Y-%m-%dT%H:%M}",
        "weekday": weekday_name(start, lang),
    }


def _appointment_view(desk: Desk, a: Appointment, lang: Lang) -> dict[str, Any]:
    view = _slot_view(desk, a.slot, lang)
    view.update(
        appointment_id=a.id,
        service=desk.clinic.service(a.service_id).name.get(lang),
        end=f"{_local(desk, a.end):%H:%M}",
    )
    return view


async def _scheduling(coro: Awaitable[Any]) -> Any:
    try:
        return await coro
    except SchedulingError as err:
        raise ToolError(err.problem.value) from None


# --- handlers ------------------------------------------------------------------------------


async def identify_patient(
    desk: Desk, state: State, a: IdentifyPatient
) -> tuple[dict[str, Any], dict[str, Any]]:
    patient = desk.patients.find_by_phone(a.phone)
    if patient is None:
        return {"found": False, "next": "Ask for their full name and call register_patient."}, {}
    history = await desk.scheduler.upcoming(patient.id)
    return {
        "found": True,
        "patient_id": patient.id,
        "name": patient.name,
        "upcoming_appointments": [_appointment_view(desk, x, state["lang"]) for x in history],
    }, {"patient_id": patient.id}


async def register_patient(
    desk: Desk, state: State, a: RegisterPatient
) -> tuple[dict[str, Any], dict[str, Any]]:
    patient = desk.patients.register(name=a.name, phone=a.phone, lang=state["lang"])
    return {
        "patient_id": patient.id,
        "name": patient.name,
        "note": "New patient: they can book a first visit or an emergency only.",
    }, {"patient_id": patient.id}


async def clinic_info(desk: Desk, state: State, a: NoArgs) -> tuple[dict[str, Any], dict[str, Any]]:
    c, lang = desk.clinic, state["lang"]
    hours = {
        WEEKDAY_NAMES[lang][WEEKDAYS.index(day)]: [f"{o:%H:%M}-{e:%H:%M}" for o, e in ranges]
        for day, ranges in c.opening_hours.items()
    }
    return {
        "name": c.name,
        "address": c.address,
        "phone": c.phone,
        "opening_hours": hours,
        "closed_dates": [d.isoformat() for d in c.closed_dates],
        "special_hours": {
            d.isoformat(): [f"{o:%H:%M}-{e:%H:%M}" for o, e in r]
            for d, r in c.special_hours.items()
        },
        "services": [
            {
                "service_id": s.id,
                "name": s.name.get(lang),
                "duration_min": s.duration_min,
                "price_eur": s.price_eur,
                "requires_prior_visit": s.requires_prior_visit,
                **({"notes": s.notes.get(lang)} if s.notes else {}),
            }
            for s in c.services
        ],
        "staff": [
            {
                "staff_id": m.id,
                "name": m.name,
                "role": m.role.get(lang),
                "days": [WEEKDAY_NAMES[lang][WEEKDAYS.index(d)] for d in m.days],
                "services": m.services,
            }
            for m in c.staff
        ],
    }, {}


async def search_policies(
    desk: Desk, state: State, a: SearchPolicies
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Placeholder retrieval (word overlap) until the RAG step compares BM25 and embeddings."""
    lang = state["lang"]
    query = set(re.findall(r"\w{4,}", a.query.lower()))

    def score(text: str) -> int:
        return len(query & set(re.findall(r"\w{4,}", text.lower())))

    ranked = sorted(
        desk.clinic.policies,
        key=lambda p: score(p.title.get(lang) + " " + p.body.get(lang)),
        reverse=True,
    )
    return {
        "results": [
            {"policy_id": p.id, "title": p.title.get(lang), "text": p.body.get(lang)}
            for p in ranked[:2]
        ]
    }, {}


async def find_slots(
    desk: Desk, state: State, a: FindSlots
) -> tuple[dict[str, Any], dict[str, Any]]:
    patient_id = _patient(state)
    today = desk.clock().astimezone(ZoneInfo(desk.clinic.timezone)).date()
    start = max(a.date_from, today)
    if a.date_to < start:
        raise ToolError("date_range_in_the_past", f"Today is {today.isoformat()}.")
    end = min(a.date_to, start + timedelta(days=MAX_SEARCH_DAYS - 1))
    found = await _scheduling(
        desk.scheduler.find_slots(
            a.service_id,
            start,
            end,
            patient_id=patient_id,
            staff_id=a.staff_id,
            time_from=a.time_from,
            time_to=a.time_to,
            limit=None,
        )
    )
    shown = found[:MAX_SLOTS_SHOWN]
    result: dict[str, Any] = {
        "searched": [start.isoformat(), end.isoformat()],
        "total_free": len(found),
        "slots": [_slot_view(desk, s, state["lang"]) for s in shown],
    }
    if not found:
        result["note"] = "No free slots in this range: offer to search other dates or times."
    elif len(found) > len(shown):
        # without this the model read the first 6 morning slots as "no afternoon slots" (FAILURES)
        result["note"] = (
            f"Showing the earliest {len(shown)} of {len(found)} free slots, up to "
            f"{_local(desk, shown[-1].start):%Y-%m-%d %H:%M}. More exist later: never say a "
            "time is unavailable without searching it, using time_from/time_to or other dates."
        )
    return result, {}


async def my_appointments(
    desk: Desk, state: State, a: NoArgs
) -> tuple[dict[str, Any], dict[str, Any]]:
    patient_id = _patient(state)
    upcoming = await desk.scheduler.upcoming(patient_id)
    return {"appointments": [_appointment_view(desk, x, state["lang"]) for x in upcoming]}, {}


_AWAITING = {
    "status": "awaiting_patient_confirmation",
    "note": "The system now reads the exact details to the patient and asks for a yes or no. "
    "Do not ask for confirmation yourself and do not say it is booked.",
}


async def propose_booking(
    desk: Desk, state: State, a: ProposeBooking
) -> tuple[dict[str, Any], dict[str, Any]]:
    patient_id = _patient(state)
    slot = Slot(service_id=a.service_id, staff_id=a.staff_id, start=_local(desk, a.start))
    await _scheduling(desk.scheduler.check_book(patient_id, slot))
    return _AWAITING, {"pending": PendingAction(kind="book", patient_id=patient_id, slot=slot)}


async def propose_move(
    desk: Desk, state: State, a: ProposeMove
) -> tuple[dict[str, Any], dict[str, Any]]:
    patient_id = _patient(state)
    current = await _scheduling(
        desk.scheduler.check_cancel(a.appointment_id, patient_id=patient_id)
    )
    slot = Slot(service_id=current.service_id, staff_id=a.staff_id, start=_local(desk, a.start))
    await _scheduling(desk.scheduler.check_move(a.appointment_id, slot, patient_id=patient_id))
    pending = PendingAction(kind="move", patient_id=patient_id, slot=slot, appointment=current)
    return _AWAITING, {"pending": pending}


async def propose_cancel(
    desk: Desk, state: State, a: ProposeCancel
) -> tuple[dict[str, Any], dict[str, Any]]:
    patient_id = _patient(state)
    current = await _scheduling(
        desk.scheduler.check_cancel(a.appointment_id, patient_id=patient_id)
    )
    pending = PendingAction(kind="cancel", patient_id=patient_id, appointment=current)
    return _AWAITING, {"pending": pending}


# --- registry ------------------------------------------------------------------------------


def _schema(model: type[_Args]) -> dict[str, Any]:
    schema = model.model_json_schema()
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema.pop("title", None)
    return schema


def _tool(name: str, description: str, args: type[_Args], handler: Handler) -> Tool:
    return Tool(ToolSpec(name, description, _schema(args)), args, handler)


TOOLS: dict[str, Tool] = {
    t.spec.name: t
    for t in (
        _tool(
            "identify_patient",
            "Find the patient by phone number. Returns their upcoming appointments.",
            IdentifyPatient,
            identify_patient,
        ),
        _tool(
            "register_patient",
            "Register a new patient (only after identify_patient found nobody).",
            RegisterPatient,
            register_patient,
        ),
        _tool(
            "clinic_info",
            "Opening hours, closed dates, services (id, duration, price) and staff (id, days).",
            NoArgs,
            clinic_info,
        ),
        _tool(
            "search_policies",
            "Search the clinic's policies (cancellation, payments, insurance, first visit,"
            " emergencies, minors, privacy, location...).",
            SearchPolicies,
            search_policies,
        ),
        _tool(
            "find_slots",
            "Free slots for a service between two dates, earliest first."
            " Needs an identified patient.",
            FindSlots,
            find_slots,
        ),
        _tool(
            "my_appointments",
            "The identified patient's upcoming appointments, with their ids.",
            NoArgs,
            my_appointments,
        ),
        _tool(
            "propose_booking",
            "Propose booking one slot from find_slots."
            " Does not book: the system asks the patient to confirm.",
            ProposeBooking,
            propose_booking,
        ),
        _tool(
            "propose_move",
            "Propose moving one of the patient's appointments to a slot from find_slots."
            " Does not move it yet.",
            ProposeMove,
            propose_move,
        ),
        _tool(
            "propose_cancel",
            "Propose cancelling one of the patient's appointments. Does not cancel it yet.",
            ProposeCancel,
            propose_cancel,
        ),
    )
}
SPECS: list[ToolSpec] = [t.spec for t in TOOLS.values()]


async def run_tool(desk: Desk, state: State, call: ToolCall) -> tuple[ToolResult, dict[str, Any]]:
    """One tool call -> its result for the model, and the state updates it causes."""

    def error(code: str, detail: str = "") -> tuple[ToolResult, dict[str, Any]]:
        body = {"error": code, **({"detail": detail} if detail else {})}
        return ToolResult(
            call.id, call.name, json.dumps(body, ensure_ascii=False), is_error=True
        ), {}

    tool = TOOLS.get(call.name)
    if tool is None:
        return error("unknown_tool", f"Available: {', '.join(TOOLS)}")
    if call.bad_arguments is not None:
        return error("arguments_not_json")
    try:
        args = tool.args.model_validate(call.arguments)
    except ValidationError as err:
        return error(
            "bad_arguments",
            "; ".join(e["msg"] + f" ({'.'.join(map(str, e['loc']))})" for e in err.errors()),
        )
    # a new proposal may replace a pending one only while the patient is answering it with
    # something other than yes/no ("sí, pero a las 11"); otherwise it is one at a time
    replacing = state["confirmation"] == "unclear"
    if call.name.startswith("propose_") and state["pending"] is not None and not replacing:
        return error("one_proposal_at_a_time")
    try:
        payload, updates = await tool.handler(desk, state, args)
    except ToolError as err:
        return error(err.code, err.detail)
    return ToolResult(call.id, call.name, json.dumps(payload, ensure_ascii=False)), updates
