"""The Google Calendar adapter and the OAuth flow against a fake Google (httpx.MockTransport):
no network, no account. The same port contract runs on the fake store and on this adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest

from clinic_desk.scheduling.fake import load_seed
from clinic_desk.scheduling.google_calendar import (
    CalendarError,
    GoogleCalendarStore,
    check_test_calendar,
)
from clinic_desk.scheduling.google_oauth import (
    TOKEN_URI,
    Client,
    OAuthError,
    Token,
    TokenSource,
    authorize,
    pkce,
)
from clinic_desk.scheduling.models import Appointment
from clinic_desk.scheduling.port import AppointmentStore
from clinic_desk.scheduling.scheduler import Scheduler, SchedulingError
from tests.test_scheduling import CLINIC, NOW, at, slot

CAL = "abc123@group.calendar.google.com"
CLIENT = Client(id="cid.apps.googleusercontent.com", secret="s3cret")


class FakeGoogle:
    """Just enough of Calendar v3 events and the token endpoint, with Google's semantics:
    timeMin/timeMax overlap, privateExtendedProperty filters (all must match), pages, 401/404."""

    def __init__(self, page_size: int = 2) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.valid = {"at-1"}
        self.page_size = page_size
        self.token_posts: list[dict[str, str]] = []
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URI:
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_posts.append(form)
            new = f"at-{len(self.token_posts) + 1}"
            self.valid = {new}
            body: dict[str, Any] = {"access_token": new, "expires_in": 3600}
            if form["grant_type"] == "authorization_code":
                body["refresh_token"] = "rt-1"
            return httpx.Response(200, json=body)
        self.calls += 1
        if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self.valid:
            return httpx.Response(401, json={"error": "invalid_credentials"})
        path = unquote(request.url.path)
        prefix = f"/calendar/v3/calendars/{CAL}/events"
        assert path.startswith(prefix), path
        event_id = path[len(prefix) + 1 :]
        if request.method == "POST":
            event = json.loads(request.content) | {"id": f"evt{len(self.events) + 1:03d}"}
            self.events[event["id"]] = event
            return httpx.Response(200, json=event)
        if event_id:
            if event_id not in self.events:
                return httpx.Response(404, json={"error": "notFound"})
            if request.method == "PUT":
                self.events[event_id] = json.loads(request.content) | {"id": event_id}
            return httpx.Response(200, json=self.events[event_id])
        return self._list(request)

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        found = sorted(self.events.values(), key=lambda e: e["start"]["dateTime"])
        if "timeMin" in params:
            low = datetime.fromisoformat(params["timeMin"])
            found = [e for e in found if datetime.fromisoformat(e["end"]["dateTime"]) > low]
        if "timeMax" in params:
            high = datetime.fromisoformat(params["timeMax"])
            found = [e for e in found if datetime.fromisoformat(e["start"]["dateTime"]) < high]
        for wanted in params.get_list("privateExtendedProperty"):
            k, v = wanted.split("=", 1)
            found = [e for e in found if e["extendedProperties"]["private"].get(k) == v]
        start = int(params.get("pageToken") or 0)
        page = found[start : start + self.page_size]
        body: dict[str, Any] = {"items": page}
        if start + self.page_size < len(found):
            body["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=body)


def google_store(google: FakeGoogle, clock: Callable[[], datetime] | None = None) -> Any:
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    token = Token(access_token="at-1", refresh_token="rt-1", expires_at=NOW + timedelta(hours=1))
    tokens = TokenSource(CLIENT, token, http, path=None, clock=clock or (lambda: NOW))
    return GoogleCalendarStore(CAL, tokens, http)


def seeded_google() -> tuple[GoogleCalendarStore, FakeGoogle, dict[str, str]]:
    """The seed's appointments written through the adapter; seed id -> event id."""
    _, fake = load_seed(CLINIC)
    google = FakeGoogle()
    store = google_store(google)

    async def copy() -> dict[str, str]:
        return {a.id: (await store.insert(a)).id for a in fake.snapshot().values()}

    return store, google, asyncio.run(copy())


@pytest.fixture(params=["fake", "google"])
def store(request: pytest.FixtureRequest) -> Iterator[AppointmentStore]:
    if request.param == "fake":
        yield load_seed(CLINIC)[1]
    else:
        yield seeded_google()[0]


# --- the port contract, on both stores ---------------------------------------------------------


def _ids(store: AppointmentStore, found: list[Appointment]) -> list[tuple[str, str]]:
    return [(a.patient_id, a.start.strftime("%m-%d %H:%M")) for a in found]


def test_between_finds_overlaps_including_the_buffer(store: AppointmentStore) -> None:
    # A001: extraction with Puig, Mon 16 Nov 12:00, visit to 12:30, blocked to 12:45
    found = asyncio.run(store.between(at("2026-11-16", "12:40"), at("2026-11-16", "13:00")))
    assert [(a.service_id, a.staff_id) for a in found] == [("extraction", "puig")]
    assert asyncio.run(store.between(at("2026-11-16", "12:45"), at("2026-11-16", "13:00"))) == []


def test_insert_assigns_an_id_and_get_reads_it_back(store: AppointmentStore) -> None:
    template = next(
        iter(asyncio.run(store.between(at("2026-11-16", "00:00"), at("2026-12-31", "23:59"))))
    )
    stored = asyncio.run(store.insert(template.model_copy(update={"id": "ignored"})))
    assert stored.id != "ignored"
    assert asyncio.run(store.get(stored.id)) == stored
    assert asyncio.run(store.get("no-such-id")) is None


def test_a_cancelled_appointment_frees_the_slot_but_stays_in_the_history(
    store: AppointmentStore,
) -> None:
    window = (at("2026-11-16", "12:00"), at("2026-11-16", "12:45"))
    [a] = asyncio.run(store.between(*window))
    asyncio.run(store.update(a.model_copy(update={"status": "cancelled"})))
    assert asyncio.run(store.between(*window)) == []
    history = asyncio.run(store.for_patient(a.patient_id))
    assert any(h.id == a.id and h.status == "cancelled" for h in history)
    assert [h.start for h in history] == sorted(h.start for h in history)


def test_updating_an_unknown_appointment_raises(store: AppointmentStore) -> None:
    [a] = asyncio.run(store.between(at("2026-11-16", "12:00"), at("2026-11-16", "12:45")))
    with pytest.raises(KeyError):
        asyncio.run(store.update(a.model_copy(update={"id": "no-such-id"})))


def test_both_stores_hold_the_same_appointments() -> None:
    fake = load_seed(CLINIC)[1]
    google = seeded_google()[0]
    window = (at("2026-11-01", "00:00"), at("2027-01-31", "23:59"))
    strip = lambda xs: [a.model_copy(update={"id": ""}) for a in xs]  # noqa: E731
    assert strip(asyncio.run(google.between(*window))) == strip(asyncio.run(fake.between(*window)))


# --- the scheduler on Google: the rules still live in the scheduler ----------------------------


def test_the_scheduler_books_moves_and_cancels_on_google() -> None:
    store, google, ids = seeded_google()
    patients, _ = load_seed(CLINIC)
    scheduler = Scheduler(CLINIC, store, patients.get, lambda: NOW)

    async def story() -> None:
        booked = await scheduler.book("p01", slot("checkup", "puig", "2026-11-18", "10:00"))
        assert google.events[booked.id]["summary"] == "checkup · puig"
        with pytest.raises(SchedulingError):  # the same slot again: the scheduler refuses
            await scheduler.book("p02", slot("checkup", "puig", "2026-11-18", "10:00"))
        moved = await scheduler.move(booked.id, slot("checkup", "puig", "2026-11-18", "11:00"))
        assert moved.start == at("2026-11-18", "11:00")
        await scheduler.cancel(ids["A001"])
        event = google.events[ids["A001"]]
        assert event["summary"].startswith("[cancelled]") and event["transparency"] == "transparent"

    asyncio.run(story())


# --- safety and tokens -------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["primary", "pablo@example.com", ""])
def test_only_a_dedicated_test_calendar_is_accepted(bad: str) -> None:
    with pytest.raises(CalendarError, match="secondary calendar"):
        check_test_calendar(bad)


def test_an_expiring_token_is_refreshed_and_saved(tmp_path: Path) -> None:
    google = FakeGoogle()
    now = [NOW]
    store = google_store(google, clock=lambda: now[0])
    store.tokens.path = tmp_path / "token.json"
    asyncio.run(store.between(at("2026-11-16", "00:00"), at("2026-11-16", "23:59")))
    assert google.token_posts == []  # valid for an hour: no refresh
    now[0] = NOW + timedelta(minutes=59, seconds=30)  # inside the last minute
    asyncio.run(store.between(at("2026-11-16", "00:00"), at("2026-11-16", "23:59")))
    [post] = google.token_posts
    assert post["grant_type"] == "refresh_token" and post["refresh_token"] == "rt-1"
    saved = Token.load(tmp_path / "token.json")
    assert saved.access_token == "at-2" and saved.refresh_token == "rt-1"  # old refresh kept


def test_a_revoked_token_gets_one_forced_refresh() -> None:
    google = FakeGoogle()
    store = google_store(google)
    google.valid = set()  # revoked: every call is 401 until a refresh
    assert asyncio.run(store.get("evt001")) is None  # 404 after the refresh, not 401
    assert len(google.token_posts) == 1


# --- the consent flow --------------------------------------------------------------------------


def test_pkce_challenge_is_the_verifiers_sha256() -> None:
    verifier, challenge = pkce()
    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def browser(
    answer: Callable[[dict[str, str]], dict[str, str]],
) -> tuple[Callable[[str], None], list[str]]:
    """Plays the user's browser: reads the consent URL, then hits the loopback redirect."""
    opened: list[str] = []

    def open_url(url: str) -> None:
        opened.append(url)
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}

        async def follow() -> None:
            await asyncio.sleep(0)
            async with httpx.AsyncClient() as real:  # the local server only: 127.0.0.1
                await real.get(q["redirect_uri"] + "/favicon.ico")  # ignored
                await real.get(q["redirect_uri"], params=answer(q))

        asyncio.get_running_loop().create_task(follow())

    return open_url, opened


def test_the_consent_flow_checks_state_and_sends_the_verifier() -> None:
    google = FakeGoogle()
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    open_url, opened = browser(lambda q: {"code": "auth-code", "state": q["state"]})
    token = asyncio.run(
        authorize(CLIENT, http, open_url, clock=lambda: datetime(2026, 11, 15, tzinfo=UTC))
    )
    assert token.refresh_token == "rt-1" and token.access_token == "at-2"
    q = parse_qs(urlsplit(opened[0]).query)
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert q["scope"] == ["https://www.googleapis.com/auth/calendar.events"]
    assert q["redirect_uri"][0].startswith("http://127.0.0.1:")
    [post] = google.token_posts
    assert post["code"] == "auth-code" and post["redirect_uri"] == q["redirect_uri"][0]
    assert "code_verifier" not in q  # the verifier never travels through the browser
    digest = hashlib.sha256(post["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"][0]


def test_a_redirect_with_another_state_is_refused() -> None:
    google = FakeGoogle()
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    open_url, _ = browser(lambda q: {"code": "stolen", "state": "someone-else"})
    with pytest.raises(OAuthError, match="state mismatch"):
        asyncio.run(authorize(CLIENT, http, open_url))
    assert google.token_posts == []  # the code was never exchanged


def test_a_refused_consent_is_reported() -> None:
    google = FakeGoogle()
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    open_url, _ = browser(lambda q: {"error": "access_denied", "state": q["state"]})
    with pytest.raises(OAuthError, match="access_denied"):
        asyncio.run(authorize(CLIENT, http, open_url))
