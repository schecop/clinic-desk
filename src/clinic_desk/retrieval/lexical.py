"""Lexical retrievers: the old word-overlap placeholder (the floor) and BM25.

BM25 is written out here (about 30 lines) instead of taken from a library: the corpus is 20
chunks, and the point of this step is to see why it beats plain overlap. Two differences do the
work: rare words weigh more than common ones (idf), and a word repeated in one chunk counts less
each time (k1), with long chunks normalised (b).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

from clinic_desk.retrieval.base import Chunk, best_per_policy

# Function words of both languages, after accent stripping. Without them, "de", "la" and "que"
# match every chunk and drown the words that matter.
STOPWORDS = frozenset(
    """
    a al als amb com con de del dels el els em en es et hi ho i la las le les li lo los me mi
    no o para pero per por que qu se si su te un una uns unes y yo ens us seu seva meu meva
    mes muy molt ya ja este esta aquest aquesta eso aixo
    """.split()  # noqa: SIM905 (a word list reads better than 60 quoted strings)
)


def tokenize(text: str, stem: int | None = 5) -> list[str]:
    """Lowercase, no accents, no stopwords; `stem` keeps a word's first N letters.

    A crude stemmer, but Spanish and Catalan put most of a word's form in its ending:
    cancelar / cancelación / cancel·lar all become "cance", cobráis / cobra become "cobra".
    """
    text = unicodedata.normalize("NFKD", text.lower().replace("l·l", "ll"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in STOPWORDS]
    return [w[:stem] for w in words] if stem else words


class Overlap:
    """The D2 placeholder: count shared words of 4+ letters. Kept as the floor to beat."""

    name = "overlap"

    def __init__(self, chunks: list[Chunk]) -> None:
        self._chunks = [(c.policy_id, set(re.findall(r"\w{4,}", c.text.lower()))) for c in chunks]

    async def prepare(self, queries: Sequence[str]) -> None:
        return None

    def rank(self, query: str) -> list[str]:
        q = set(re.findall(r"\w{4,}", query.lower()))
        return best_per_policy([(pid, float(len(q & words))) for pid, words in self._chunks])


class BM25:
    """Okapi BM25 over the chunks; a policy scores as its best chunk."""

    def __init__(
        self, chunks: list[Chunk], stem: int | None = 5, k1: float = 1.2, b: float = 0.75
    ) -> None:
        self.name = f"bm25{f'-stem{stem}' if stem else ''}"
        self._stem, self._k1, self._b = stem, k1, b
        self._ids = [c.policy_id for c in chunks]
        self._tf = [Counter(tokenize(c.text, stem)) for c in chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = sum(self._len) / len(self._len)
        n = len(chunks)
        df = Counter(word for tf in self._tf for word in tf)
        # the "+1" form keeps idf positive even for a word in most chunks
        self._idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = tokenize(query, self._stem)
        out = []
        for tf, length in zip(self._tf, self._len, strict=True):
            norm = self._k1 * (1 - self._b + self._b * length / self._avg)
            out.append(
                sum(
                    self._idf.get(t, 0.0) * tf[t] * (self._k1 + 1) / (tf[t] + norm)
                    for t in terms
                    if t in tf
                )
            )
        return out

    async def prepare(self, queries: Sequence[str]) -> None:
        return None

    def rank(self, query: str) -> list[str]:
        return best_per_policy(list(zip(self._ids, self.scores(query), strict=True)))
