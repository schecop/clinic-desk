"""The retrieval interface: a query in, policy ids out, best first.

Both language versions of a policy are indexed as separate chunks, and a policy scores as its
best chunk. So the query's language does not have to match the patient's: in the second real
chat the model wrote its `search_policies` query in Spanish for a Catalan patient. The tool
then returns the text in the patient's language.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from clinic_desk.clinic import Clinic, Lang


@dataclass(frozen=True)
class Chunk:
    policy_id: str
    lang: Lang
    text: str  # title + body


def chunks_from(clinic: Clinic) -> list[Chunk]:
    return [
        Chunk(p.id, lang, f"{p.title.get(lang)}. {p.body.get(lang)}")
        for p in clinic.policies
        for lang in clinic.languages
    ]


class Retriever(Protocol):
    name: str

    async def prepare(self, queries: Sequence[str]) -> None:
        """Work that needs I/O before `rank` (embedding the queries); a no-op for BM25."""
        ...

    def rank(self, query: str) -> list[str]:
        """Every policy id, best first. Call `prepare` with the query first."""
        ...


def best_per_policy(scored: list[tuple[str, float]]) -> list[str]:
    """Policy ids ordered by their best chunk's score (ties keep corpus order)."""
    best: dict[str, float] = {}
    for policy_id, score in scored:
        best[policy_id] = max(score, best.get(policy_id, float("-inf")))
    return sorted(best, key=lambda pid: best[pid], reverse=True)
