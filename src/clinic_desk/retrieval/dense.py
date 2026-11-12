"""Dense retrieval: embed chunks and query, rank by cosine similarity.

Embeddings match meaning, not words, which is what the lexical retrievers lacked on colloquial
questions ("Tengo Sanitas" vs "aseguradora"). The embedder is any async function from texts to
vectors; `openai_embedder` calls the OpenAI-compatible /embeddings endpoint with the shared
retrying POST. Vectors are cached on disk by model and text, so a re-run costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import httpx

from clinic_desk.llm.http import post_json
from clinic_desk.retrieval.base import Chunk, best_per_policy

Embedder = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


def openai_embedder(
    client: httpx.AsyncClient, model: str, usage: list[int] | None = None
) -> Embedder:
    """`usage`, if given, collects the input tokens of every call (for the cost)."""

    async def embed(texts: Sequence[str]) -> list[list[float]]:
        body, _ = await post_json(client, "/embeddings", {"model": model, "input": list(texts)})
        if usage is not None:
            usage.append(int(body.get("usage", {}).get("prompt_tokens", 0)))
        data = sorted(body["data"], key=lambda d: d["index"])
        return [list(map(float, d["embedding"])) for d in data]

    return embed


def cached(embed: Embedder, path: Path) -> Embedder:
    """Wrap an embedder with a JSON cache keyed by the text's hash."""

    def key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    async def run(texts: Sequence[str]) -> list[list[float]]:
        store: dict[str, list[float]] = json.loads(path.read_text("utf-8")) if path.exists() else {}
        missing = [t for t in dict.fromkeys(texts) if key(t) not in store]
        if missing:
            for text, vector in zip(missing, await embed(missing), strict=True):
                store[key(text)] = vector
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(store), "utf-8")
        return [store[key(t)] for t in texts]

    return run


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


class Dense:
    """Cosine ranking over precomputed vectors. Build with `await Dense.build(...)`, then call
    `await prepare(queries)` before `rank`, so ranking itself stays synchronous like BM25."""

    def __init__(self, name: str, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        self.name = name
        self._embed: Embedder | None = None
        self._ids = [c.policy_id for c in chunks]
        self._vectors = vectors
        self._queries: dict[str, list[float]] = {}

    @classmethod
    async def build(cls, name: str, chunks: list[Chunk], embed: Embedder) -> Dense:
        dense = cls(name, chunks, await embed([c.text for c in chunks]))
        dense._embed = embed
        return dense

    async def prepare(self, queries: Sequence[str]) -> None:
        assert self._embed is not None
        todo = [q for q in queries if q not in self._queries]
        if todo:
            self._queries.update(zip(todo, await self._embed(todo), strict=True))

    def rank(self, query: str) -> list[str]:
        q = self._queries[query]
        return best_per_policy(
            [(pid, cosine(q, v)) for pid, v in zip(self._ids, self._vectors, strict=True)]
        )
