"""The calendar port on Google Calendar: one event per appointment, on a dedicated test calendar.

Mapping:
- The event spans `start` → `block_end` (the staff member is blocked for the buffer too), so
  Google's own time filter finds every appointment that overlaps a window.
- The whole appointment (minus its id) is kept as JSON in a private extended property; the
  event id is the appointment id. Private properties are invisible to other apps and can be
  filtered on (`patient_id`, `clinic_desk`), which `for_patient` and `between` use.
- A cancelled appointment keeps its event, marked cancelled in the JSON and in the title, and
  made transparent (it no longer shows as busy). The port's history keeps it, as the fake does.

Safety (CLAUDE.md): only a secondary calendar (`…@group.calendar.google.com`) is accepted, never
"primary" or an address, which is somebody's own calendar.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from clinic_desk.scheduling.models import Appointment

API = "https://www.googleapis.com/calendar/v3"
MARK = "clinic_desk"  # private property on every event this adapter writes


class Tokens(Protocol):
    async def access_token(self, force: bool = False) -> str: ...


class CalendarError(RuntimeError):
    pass


def check_test_calendar(calendar_id: str) -> str:
    if not calendar_id.endswith("@group.calendar.google.com"):
        raise CalendarError(
            f"{calendar_id!r} is not a secondary calendar: create a dedicated test calendar and"
            " use its id (…@group.calendar.google.com), never 'primary' or a personal one"
        )
    return calendar_id


def to_event(a: Appointment) -> dict[str, Any]:
    cancelled = a.status == "cancelled"
    return {
        "summary": f"{'[cancelled] ' if cancelled else ''}{a.service_id} · {a.staff_id}",
        "description": "Clinic Desk test data (synthetic patients).",
        "start": {"dateTime": a.start.isoformat()},
        "end": {"dateTime": a.block_end.isoformat()},
        "transparency": "transparent" if cancelled else "opaque",
        "extendedProperties": {
            "private": {
                MARK: "1",
                "patient_id": a.patient_id,
                "appointment": a.model_dump_json(exclude={"id"}),
            }
        },
    }


def from_event(event: dict[str, Any]) -> Appointment | None:
    """The appointment an event stores, or None for an event this adapter did not write."""
    private = event.get("extendedProperties", {}).get("private", {})
    if private.get(MARK) != "1" or "appointment" not in private:
        return None
    return Appointment.model_validate(json.loads(private["appointment"]) | {"id": event["id"]})


class GoogleCalendarStore:
    def __init__(self, calendar_id: str, tokens: Tokens, http: httpx.AsyncClient) -> None:
        self.calendar_id = check_test_calendar(calendar_id)
        self.tokens, self.http = tokens, http
        self._events = f"{API}/calendars/{quote(calendar_id, safe='')}/events"

    async def _call(self, method: str, url: str, **kw: Any) -> httpx.Response:
        for attempt in (1, 2):  # a 401 forces one refresh: the token may have been revoked
            token = await self.tokens.access_token(force=attempt == 2)
            response = await self.http.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, **kw
            )
            if response.status_code != 401:
                return response
        return response

    async def _list(self, params: list[tuple[str, str]]) -> list[Appointment]:
        found: list[Appointment] = []
        page = ""
        while True:
            query = [*params, ("singleEvents", "true"), ("maxResults", "250")]
            query += [("pageToken", page)] if page else []
            response = await self._call("GET", self._events, params=query)
            self._raise(response)
            body = response.json()
            found += [a for e in body.get("items", []) if (a := from_event(e))]
            page = body.get("nextPageToken", "")
            if not page:
                return sorted(found, key=lambda a: a.start)

    def _raise(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise CalendarError(f"Google Calendar {response.status_code}: {response.text[:300]}")

    async def between(self, start: datetime, end: datetime) -> list[Appointment]:
        listed = await self._list(
            [
                ("timeMin", start.isoformat()),
                ("timeMax", end.isoformat()),
                ("privateExtendedProperty", f"{MARK}=1"),
            ]
        )
        # Google filters on the event's own span; the port's rule is checked again here
        return [a for a in listed if a.status == "booked" and a.start < end and start < a.block_end]

    async def for_patient(self, patient_id: str) -> list[Appointment]:
        return await self._list(
            [
                ("privateExtendedProperty", f"{MARK}=1"),
                ("privateExtendedProperty", f"patient_id={patient_id}"),
            ]
        )

    async def get(self, appointment_id: str) -> Appointment | None:
        response = await self._call("GET", f"{self._events}/{quote(appointment_id, safe='')}")
        if response.status_code in (404, 410):
            return None
        self._raise(response)
        return from_event(response.json())

    async def insert(self, appointment: Appointment) -> Appointment:
        response = await self._call("POST", self._events, json=to_event(appointment))
        self._raise(response)
        return appointment.model_copy(update={"id": response.json()["id"]})

    async def update(self, appointment: Appointment) -> None:
        url = f"{self._events}/{quote(appointment.id, safe='')}"
        response = await self._call("PUT", url, json=to_event(appointment))
        if response.status_code in (404, 410):
            raise KeyError(appointment.id)
        self._raise(response)
