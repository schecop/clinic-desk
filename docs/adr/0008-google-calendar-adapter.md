# ADR-0008: Google Calendar adapter with hand-written OAuth

- **Date:** 2026-11-15
- **Status:** accepted

## Context
The calendar is a port (`AppointmentStore`: `between`, `for_patient`, `get`, `insert`, `update`). Simulations run on the in-memory fake. The demo, and the week's OAuth practice, need a real calendar. Safety rule: only a dedicated test calendar, never a personal one, with a scope limited to calendar events.

## Decision
- **OAuth written by hand with httpx** (Pablo), as an installed app ("Desktop app" client): consent in the browser, redirect to a one-shot server on `127.0.0.1` at a free port, **PKCE (S256)** and a random **state** that must match, code exchanged for access and refresh tokens, `token.json` (gitignored). `TokenSource` refreshes the access token a minute before it expires (one refresh when two calls race) and saves it; a 401 forces one refresh and a retry. Scope: `calendar.events` only.
- **One event per appointment.** The event spans `start` → `block_end`, so Google's time filter sees the buffer. The appointment (without its id) is stored as JSON in a private extended property, and the event id is the appointment id. `between` and `for_patient` filter on private properties (`clinic_desk=1`, `patient_id`) and re-check the port's rule in code. A cancelled appointment keeps its event, marked in the title and made transparent.
- **Only a secondary calendar** (`…@group.calendar.google.com`) is accepted. "primary" or an address, which is somebody's own calendar, is refused before consent is asked.
- Commands: `calendar-auth` (consent), `calendar-seed` (copy the seed appointments once), `chat --calendar google`. Patients still come from the seed.

## Alternatives considered
- **google-auth-oauthlib for the flow, httpx for the API:** less security code of our own, but OAuth becomes a black box, which defeats the point of the exercise, and it adds 3-4 dependencies.
- **The official client (google-api-python-client):** synchronous and heavy for a 5-method async port.
- **Store the appointment in the event's description:** visible to anyone who opens the event and cannot be filtered on; private extended properties can be both hidden and filtered.

## Consequences
- The same contract tests run on the fake store and on the adapter, against a fake Google (`httpx.MockTransport`) that follows Google's overlap, filter, paging, 401 and 404 semantics; the scheduler books, moves and cancels on it. Three mutations (no state check, no cancelled filter, no 401 retry) each fail a test.
- Tried on a real test calendar (2026-11-15): consent with PKCE and state, `calendar-seed` (13 events), a read-only check that `between` (buffer boundary at 12:45), the private-property filter of `for_patient` and `get` behave as on the fake, then a `chat --calendar google` that booked a checkup and moved a cleaning, both through the confirmation gate. Token refresh is covered by tests; on the real API it happens on the first use after the hour.
- Our own OAuth code is security code to maintain. It is small (~150 lines), uses `secrets` for every random value, binds only to loopback, and never logs tokens.
