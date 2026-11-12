"""Retrieval eval: hit@1 and hit@3 per retriever over the colloquial question set.

hit@k: any relevant policy is in the top k. hit@1 is what matters most (the tool shows the top
results first and the model leans on the first); hit@3 says whether a wider cut would rescue it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from clinic_desk.clinic import Clinic, Lang
from clinic_desk.retrieval.base import Retriever

QUESTIONS_PATH = Path("data/eval/policy_questions.yaml")


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lang: Lang
    q: str
    relevant: list[str]


def load_questions(clinic: Clinic, path: Path = QUESTIONS_PATH) -> list[Question]:
    questions = [Question.model_validate(x) for x in yaml.safe_load(path.read_text("utf-8"))]
    known = {p.id for p in clinic.policies}
    for question in questions:
        if unknown := set(question.relevant) - known:
            raise ValueError(f"unknown policy {sorted(unknown)} in {question.q!r}")
    return questions


@dataclass
class Result:
    retriever: str
    hit1: dict[Lang, int] = field(default_factory=lambda: {"es": 0, "ca": 0})
    hit3: dict[Lang, int] = field(default_factory=lambda: {"es": 0, "ca": 0})
    total: dict[Lang, int] = field(default_factory=lambda: {"es": 0, "ca": 0})
    misses: list[tuple[Question, list[str]]] = field(default_factory=list)  # not in top 1


def evaluate(retriever: Retriever, questions: list[Question]) -> Result:
    result = Result(retriever.name)
    for question in questions:
        ranked = retriever.rank(question.q)
        relevant = set(question.relevant)
        result.total[question.lang] += 1
        if ranked[0] in relevant:
            result.hit1[question.lang] += 1
        else:
            result.misses.append((question, ranked[:3]))
        if relevant & set(ranked[:3]):
            result.hit3[question.lang] += 1
    return result


def report(results: list[Result], all_in_prompt_tokens: dict[Lang, int]) -> str:
    def cell(hits: dict[Lang, int], total: dict[Lang, int]) -> str:
        return f"{sum(hits.values())}/{sum(total.values())} (es {hits['es']}, ca {hits['ca']})"

    lines = [
        "| retriever | hit@1 | hit@3 |",
        "|---|---|---|",
        *(
            f"| {r.retriever} | {cell(r.hit1, r.total)} | {cell(r.hit3, r.total)} |"
            for r in results
        ),
        "",
        "All policies in the prompt instead: no retrieval misses, about"
        f" {all_in_prompt_tokens['es']} tokens (es) / {all_in_prompt_tokens['ca']} (ca) added to"
        " every model call (estimate: characters / 4).",
    ]
    for r in results:
        if r.misses:
            lines += ["", f"Misses at 1 for {r.retriever}:"]
            lines += [f"- [{q.lang}] {q.q} → got {top}, wanted {q.relevant}" for q, top in r.misses]
    return "\n".join(lines)


def all_in_prompt_tokens(clinic: Clinic) -> dict[Lang, int]:
    return {
        lang: sum(len(p.title.get(lang)) + len(p.body.get(lang)) for p in clinic.policies) // 4
        for lang in clinic.languages
    }
