from __future__ import annotations

from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from clinic_desk.clinic import DATA_DIR, Clinic, load_clinic


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((DATA_DIR / "clinic.yaml").read_text(encoding="utf-8"))
    raw["policies"] = yaml.safe_load((DATA_DIR / "policies.yaml").read_text(encoding="utf-8"))
    return raw


def test_the_committed_clinic_loads() -> None:
    clinic = load_clinic()
    assert clinic.languages == ["es", "ca"]
    assert len(clinic.policies) == 10
    assert clinic.service("root_canal").duration_min == 90
    assert [m.id for m in clinic.staff_for("emergency")] == ["puig", "roca", "vidal"]
    assert "sat" not in clinic.opening_hours


def test_every_policy_exists_in_both_languages() -> None:
    for policy in load_clinic().policies:
        assert policy.body.get("es") != policy.body.get("ca"), policy.id


def test_a_missing_translation_fails_at_load() -> None:
    raw = _raw()
    del raw["policies"][0]["body"]["ca"]
    with pytest.raises(ValidationError, match="ca"):
        Clinic.model_validate(raw)


def test_staff_offering_an_unknown_service_fails() -> None:
    raw = _raw()
    raw["staff"][0]["services"].append("implants")
    with pytest.raises(ValidationError, match="unknown services"):
        Clinic.model_validate(raw)


def test_staff_working_on_a_closed_day_fails() -> None:
    raw = _raw()
    raw["staff"][0]["days"].append("sat")
    with pytest.raises(ValidationError, match="closed days"):
        Clinic.model_validate(raw)


def test_a_service_nobody_offers_fails() -> None:
    raw = _raw()
    raw["staff"] = [m for m in raw["staff"] if m["id"] != "soler"]
    with pytest.raises(ValidationError, match="nobody offers"):
        Clinic.model_validate(raw)


def test_reversed_opening_range_fails() -> None:
    raw = _raw()
    raw["opening_hours"]["fri"] = [["15:00", "09:00"]]
    with pytest.raises(ValidationError, match="reversed"):
        Clinic.model_validate(raw)


def test_duration_off_the_slot_grid_fails() -> None:
    raw = _raw()
    raw["services"][0]["duration_min"] = 25
    with pytest.raises(ValidationError, match="slot grid"):
        Clinic.model_validate(raw)


def test_load_without_policies_file(tmp_path: Path) -> None:
    (tmp_path / "clinic.yaml").write_text(
        (DATA_DIR / "clinic.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert load_clinic(tmp_path).policies == []


def test_hours_on_applies_closed_then_special_then_weekday() -> None:
    clinic = load_clinic()
    assert clinic.hours_on(date(2026, 12, 25)) == []  # Christmas: closed
    assert clinic.hours_on(date(2026, 12, 24)) == [(time(9), time(14))]  # Thursday, morning only
    assert clinic.hours_on(date(2026, 12, 31)) == [(time(9), time(14))]
    assert clinic.hours_on(date(2026, 12, 17)) == [(time(9), time(14)), (time(16), time(20))]
    assert clinic.hours_on(date(2026, 12, 19)) == []  # Saturday


def test_a_date_both_closed_and_special_fails() -> None:
    raw = _raw()
    raw["special_hours"]["2026-12-25"] = [["09:00", "12:00"]]
    with pytest.raises(ValidationError, match="both closed and has special hours"):
        Clinic.model_validate(raw)


def test_special_hours_are_grid_checked_too() -> None:
    raw = _raw()
    raw["special_hours"]["2026-12-24"] = [["09:00", "13:50"]]
    with pytest.raises(ValidationError, match="off the 15-min grid"):
        Clinic.model_validate(raw)


def test_buffers_only_on_long_or_overrunning_services() -> None:
    clinic = load_clinic()
    buffered = {s.id: s.buffer_min for s in clinic.services if s.buffer_min}
    assert buffered == {"extraction": 15, "root_canal": 15, "emergency": 15}


def test_buffer_off_the_slot_grid_fails() -> None:
    raw = _raw()
    raw["services"][0]["buffer_min"] = 10
    with pytest.raises(ValidationError, match="buffer off the slot grid"):
        Clinic.model_validate(raw)
