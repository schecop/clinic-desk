"""Deterministic language work: which language the patient speaks, whether they said yes, and
the exact confirmation question. No LLM: these sit on the write path, so they must be
predictable, free and testable.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Literal

from clinic_desk.clinic import Clinic, Lang

Confirmation = Literal["yes", "no", "unclear"]

# --- language ------------------------------------------------------------------------------
# Marker words only: a word both languages use ("el", "del", "una", "mal", and "es", which is a
# Catalan pronoun too) would pull a Catalan reply towards Spanish and trigger a needless
# translation.

_CA = {
    "vull",
    "voldria",
    "gràcies",
    "moltes",
    "dilluns",
    "dimarts",
    "dimecres",
    "dijous",
    "divendres",
    "demà",
    "avui",
    "sisplau",
    "perquè",
    "també",
    "amb",
    "meva",
    "meu",
    "seva",
    "puc",
    "bon",
    "bona",
    "tinc",
    "perfecte",
    "adéu",
    "sóc",
    "em",
    "és",
    "què",
    "molt",
    "fer",
    "vostè",
    "queixal",
    "setmana",
    "propera",
    "pròxima",
    "aquesta",
    "aquest",
    "anul·lar",
    "canviar",
    "doncs",
    "però",
    "res",
    "gens",
    "hi",
    "dir",
    "dic",
    "nom",
    "us",
    "plau",
    "li",
    "pot",
    "està",
    "té",
    "vol",
    "ja",
    "així",
    "quan",
    "després",
    "llavors",
    "necessita",
    "truqui",
    "ara",
    "dia",
    "dies",
    "matí",
    "preu",
    "hores",
    "minuts",
    "forat",
    "seu",
    "cancel·lació",
    "aleshores",
}
_ES = {
    "quiero",
    "quisiera",
    "gracias",
    "muchas",
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "mañana",
    "hoy",
    "favor",
    "porque",
    "también",
    "con",
    "mi",
    "puedo",
    "tengo",
    "buenos",
    "buenas",
    "días",
    "soy",
    "llamo",
    "me",
    "muy",
    "usted",
    "muela",
    "semana",
    "próxima",
    "esta",
    "este",
    "anular",
    "cambiar",
    "pues",
    "pero",
    "nada",
    "vale",
    "qué",
    "hacer",
    "duele",
    "nombre",
    "cita",
    "para",
    "y",
    "su",
    "sus",
    "le",
    "puede",
    "está",
    "hay",
    "tiene",
    "quiere",
    "ya",
    "así",
    "cuándo",
    "después",
    "entonces",
    "necesita",
    "llame",
    "ahora",
    "día",
    "precio",
    "horas",
    "minutos",
    "hueco",
    "cancelación",
}


def _marker_counts(text: str) -> tuple[int, int]:
    words = re.findall(r"[\w·']+", text.lower())
    return sum(w in _CA for w in words), sum(w in _ES for w in words)


def guess_lang(text: str, default: Lang) -> Lang:
    """Spanish or Catalan by marker words; the current language when the text is too short."""
    ca, es = _marker_counts(text)
    if ca > es:
        return "ca"
    if es > ca:
        return "es"
    return default


def clear_lang(text: str, margin: int = 2) -> Lang | None:
    """The language of a reply only when the markers say so by a clear margin, else None.

    Stricter than `guess_lang` on purpose: it decides whether to translate the model's reply,
    and a false alarm costs a model call and risks rewording a reply that was fine.
    """
    ca, es = _marker_counts(text)
    if ca - es >= margin:
        return "ca"
    if es - ca >= margin:
        return "es"
    return None


def numbers_in(text: str) -> list[str]:
    """Every digit run, in order: what a translation must keep (times, dates, prices, phones)."""
    return re.findall(r"\d+", text)


# --- confirmation --------------------------------------------------------------------------

_YES = (
    "si",
    "vale",
    "ok",
    "okay",
    "perfecto",
    "perfecte",
    "correcto",
    "correcte",
    "de acuerdo",
    "d'acord",
    "dacord",
    "confirmo",
    "confirmado",
    "confirmat",
    "adelante",
    "endavant",
    "claro",
    "es clar",
    "exacto",
    "exacte",
    "muy bien",
    "molt be",
    "genial",
    "estupendo",
    "i tant",
    "por supuesto",
    "va be",
    "entesos",
)
_FILLERS = ("pues", "bueno", "doncs", "eh", "ah", "hmm", "mmm")
_HEDGES = {
    "no",
    "pero",
    "mejor",
    "millor",
    "otro",
    "otra",
    "altre",
    "altra",
    "espera",
    "espere",
    "esperi",
    "quiza",
    "quizas",
    "potser",
    "antes",
    "abans",
    "despues",
    "despres",
    "tarde",
    "tarda",
    "puede",
    "pot",
    "podria",
    "cambiar",
    "canviar",
    "cambia",
    "canvia",
    "dudo",
}


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def classify_confirmation(text: str) -> Confirmation:
    """'yes' only for a short, plain yes; anything mixed or doubtful is 'unclear'.

    Errs on the safe side: "sí, pero mejor a las 11" is not a yes. A missed yes costs one more
    question; a false yes writes a slot the patient did not want.
    """
    plain = _plain(text).strip()
    words = re.findall(r"[a-z']+", plain)
    if not words:
        return "unclear"
    if words[0] == "no" or (set(words) & {"no"} and not set(words) & {"si"}):
        return "no"
    if "?" in plain or re.search(r"\d", plain) or len(words) > 10 or set(words) & _HEDGES:
        return "unclear"
    while len(words) > 1 and words[0] in _FILLERS:
        words = words[1:]
    head = " ".join(words)
    if any(head == y or head.startswith(y + " ") for y in _YES):
        return "yes"
    return "unclear"


# --- rendering -----------------------------------------------------------------------------

WEEKDAY_NAMES: dict[Lang, tuple[str, ...]] = {
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "ca": ("dilluns", "dimarts", "dimecres", "dijous", "divendres", "dissabte", "diumenge"),
}
_MONTHS: dict[Lang, tuple[str, ...]] = {
    "es": (
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ),
    "ca": (
        "gener",
        "febrer",
        "març",
        "abril",
        "maig",
        "juny",
        "juliol",
        "agost",
        "setembre",
        "octubre",
        "novembre",
        "desembre",
    ),
}


def weekday_name(day: datetime, lang: Lang) -> str:
    return WEEKDAY_NAMES[lang][day.weekday()]


def day_label(day: date, lang: Lang) -> str:
    """'lunes 16 de noviembre' / 'dilluns 16 de novembre' / 'dijous 1 d'octubre'."""
    month = _MONTHS[lang][day.month - 1]
    of = "d'" if lang == "ca" and month[0] in "aeiou" else "de "
    return f"{WEEKDAY_NAMES[lang][day.weekday()]} {day.day} {of}{month}"


def when(moment: datetime, lang: Lang) -> str:
    """'lunes 16 de noviembre a las 09:00' / 'dilluns 16 de novembre a les 09:00'."""
    return f"{day_label(moment.date(), lang)} {'a las' if lang == 'es' else 'a les'} {moment:%H:%M}"


def with_staff(clinic: Clinic, staff_id: str, lang: Lang) -> str:
    """'con el Dr. Pau Roca' / 'amb la Dra. Marta Puig' / 'con Laia Soler'."""
    name = next(m.name for m in clinic.staff if m.id == staff_id)
    article = "el " if name.startswith("Dr. ") else "la " if name.startswith("Dra. ") else ""
    return f"{'con' if lang == 'es' else 'amb'} {article}{name}"
