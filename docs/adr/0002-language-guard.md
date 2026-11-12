# ADR-0002: Language guard by translation, checked by code

- **Date:** 2026-11-12
- **Status:** accepted

## Context
The patient may switch from Spanish to Catalan mid-call. In both real chats the receptionist model (Haiku 5.5) answered the switch turn in Spanish, even with the reply language as the last line of the context block (FAILURES, 2026-11-11). A prompt line is not enough.

## Decision
After the agent's final reply, code checks its language (`clear_lang`: marker words, margin of 2). If it is clearly the other language, a second call translates it (`receptionist/translate.py`). The translation is kept only if it has the same numbers as the original (times, dates, prices, phones) and is not still in the wrong language. Otherwise the original reply is spoken: a right fact in the wrong language beats a wrong fact in the right one. The translator model is the `translator` role in config and falls back to the receptionist's. `state["lang_fix"]` records `translated` or `kept` for the trace and the simulator.

## Alternatives considered
- **Retry the receptionist with a warning:** regenerates the whole reply, so slots or prices can change along with the language, and it repeats the long prompt with tools (slower, dearer). Proposed first; Pablo chose translation.
- **Language tag in the user message:** cheap, but a hint again, like the prompt line that already failed.
- **Leave it to the coach:** a good demo case, but the product is wrong until then.

## Consequences
- One extra short call only on wrong-language turns, with no tools and no long prompt.
- The detector is a word list: it misses a reply with few marker words (stays in the wrong language) and can misfire on mixed text. Marker words both languages use were removed. The simulator's code-checked language criterion will measure both.
- Not covered: a Spanish word inside a Catalan reply ("hueco"), or a `search_policies` query written in Spanish. The judge scores the first; RAG has to cope with the second.
