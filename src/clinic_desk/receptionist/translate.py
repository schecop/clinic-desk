"""The language guard: a reply in the wrong language is translated, not regenerated.

The receptionist model sometimes answers a Catalan question in Spanish (FAILURES, 2 of 2 on the
switch turn), and a prompt line did not fix it. Regenerating the reply could change its facts
(slots, prices) along with its language; translating keeps what the tools returned and only
changes the words. The translation is checked by code: same numbers, right language. If the
check fails, the original reply is kept: a right fact in the wrong language beats a wrong fact
in the right one.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Literal

from clinic_desk.clinic import Lang
from clinic_desk.llm.base import LLMClient, UserMessage
from clinic_desk.receptionist.language import clear_lang, numbers_in

LangFix = Literal["translated", "kept"]  # kept: translation failed its check, original spoken

_NAME: dict[Lang, str] = {"es": "Spanish", "ca": "Catalan"}

PROMPT_PATH = Path("prompts/translator.md")  # placeholders: {name}, {formal}
_FORMAL: dict[Lang, str] = {"es": "usted", "ca": "vostè"}


@cache
def _template(path: Path = PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


def needs_translation(reply: str, lang: Lang) -> bool:
    """True only when the reply is clearly in the other language (see `clear_lang`)."""
    found = clear_lang(reply)
    return found is not None and found != lang


async def translate(llm: LLMClient, text: str, lang: Lang, max_tokens: int) -> str | None:
    """`text` in `lang`, or None if the translation changed a number or is still not in `lang`."""
    response = await llm.complete(
        system=_template().format(name=_NAME[lang], formal=_FORMAL[lang]),
        messages=[UserMessage(text)],
        max_tokens=max_tokens,
    )
    out = response.text.strip()
    if not out or sorted(numbers_in(out)) != sorted(numbers_in(text)):
        return None
    if clear_lang(out, margin=1) not in (None, lang):
        return None
    return out
