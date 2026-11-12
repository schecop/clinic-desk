"""Typed config from YAML. Tunables and the model per role live here, never in code (rule 7)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelChoice(_Strict):
    """Provider and model for one role (receptionist, persona, judge, coach, embeddings)."""

    provider: str  # a key of clinic_desk.llm.factory.PROVIDERS
    model: str
    # provider-specific request fields, passed through as-is (reasoning effort, thinking)
    options: dict[str, Any] = {}
    # list prices, for the cost of every call (cost per conversation, per simulator pass)
    usd_per_mtok_in: Decimal = Field(ge=0)
    usd_per_mtok_out: Decimal = Field(ge=0)


class ReceptionistSettings(_Strict):
    """How the receptionist gets the clinic's policies (both are evaluated by the simulator).

    prompt: every policy, both languages, in the fixed (cached) prompt. The default: with 10
    policies, retrieval misses cost more than ~1.5k cached tokens (docs/adr/0003).
    search: a `search_policies` tool over `retriever`: "bm25" (local, free) or an OpenAI
    embedding model name such as "text-embedding-3-small" (paid, one call per search).
    """

    policies: Literal["prompt", "search"] = "prompt"
    retriever: str = "bm25"


class AppConfig(_Strict):
    llm: dict[str, ModelChoice]
    receptionist: ReceptionistSettings = ReceptionistSettings()


def load_config(path: Path = Path("config/local.yaml")) -> AppConfig:
    """`config/local.yaml` (gitignored) if present, else the committed example."""
    if not path.exists():
        path = Path("config/example.yaml")
    return AppConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
