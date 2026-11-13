"""Does a reply say a booking, move or cancellation is done?

Used twice: by the graph's claim guard (a reply that claims a write the graph has not made is
not spoken) and by the simulator's false-claim check. Word lists, so it misses some phrasings;
the judge reads the rest.
"""

from __future__ import annotations

import re
import unicodedata


def plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


_DONE = (
    r"(reservad|cambiad|movid|anulad|cancelad|confirmad"  # es
    r"|reservat|canviat|canviad|anul.lad|anul.lat|cancel.lad|cancel.lat|confirmad|confirmat)"  # ca
)
_CLAIM = re.compile(
    r"\b(?:"
    rf"(?:ya|ja)\s+(?:esta|queda|tiene|te)\s+(?:\w+\s+){{0,3}}{_DONE}"
    rf"|queda(?:n)?\s+(?:\w+\s+){{0,2}}{_DONE}"
    rf"|(?:ha|han)\s+(?:quedado|quedat)\s+{_DONE}"
    r"|he\s+(?:reservado|cambiado|movido|anulado|cancelado|reservat|canviat|mogut|anul.lat|cancel.lat)"
    r"|(?:cita|visita)\s+(?:\w+\s+){0,3}(?:esta|ha\s+sido|ha\s+estat)\s+" + _DONE + r")"
)


def claims_write(reply: str) -> bool:
    """The reply says a booking, move or cancellation has been done."""
    return bool(_CLAIM.search(plain(reply)))
