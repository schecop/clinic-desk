# Failures

Every real failure: symptom, how it was found (trace, judge, test), root cause class (prompt, tool, retrieval, model, simulator, judge), fix, and the regression test or eval case added. Newest first.

<!-- Template:
## YYYY-MM-DD · short title
- **Symptom:**
- **Found by:**
- **Root cause (class):**
- **Fix:**
- **Regression guard:**
-->

## 2026-11-11 · The double-booking test passed without the lock
- **Symptom:** `test_double_booking_is_impossible_even_when_concurrent` stayed green after removing the scheduler's `asyncio.Lock`, so it proved nothing.
- **Found by:** mutation check by hand (remove the lock, re-run the test).
- **Root cause (class):** tool (test double). The in-memory store never suspended at an `await`, so two concurrent bookings ran one after the other. A first fix (`sleep(0)` before the read) still passed: the second booking read the list after the first had written.
- **Fix:** the fake reads first and yields after (`await asyncio.sleep(0)` before returning), like real I/O. Without the lock both bookings now succeed and the test fails.
- **Regression guard:** the same test, now verified to fail without the lock.

## 2026-11-11 · Two services off the booking grid (D1)
- **Symptom:** two 20-min services on a 15-min slot grid: unbookable or overlapping slots.
- **Found by:** the clinic loader's cross-checks, on their first run.
- **Root cause (class):** fixture data.
- **Fix:** both set to 15 min.
- **Regression guard:** `Clinic._cross_checks` rejects durations and buffers off the grid.
