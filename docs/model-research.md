# Model research (rule 7)

No default model. Each role gets the model that wins on its own job, on quality, latency and cost. Data comes from searches on 2026-11-11 (official pricing pages where possible), with the source noted, then reviewed role by role with Pablo. **Every verdict below is a hypothesis with the test that settles it:** the evals decide (ADR `model-choice`, D4).

**Budget: $15 total in paid API calls for the whole PoC** (Pablo, 2026-11-11). There are no API credits (the hackathon credits are on a separate account). This shapes every choice below: development runs on the scripted fake model, free tiers carry the roles that are not under test, and a budget guard stops any run that would cross the cap (see Budget).

## Roles in this project

| Role | What it does | Calls per conversation | What matters most |
|---|---|---|---|
| **Receptionist** | Talks to the patient in ES/CA, calls calendar and policy tools, never writes without confirmation | ~8-12 (one per turn, plus tool rounds) | Latency per turn (voice later), reliable tool use, Spanish and Catalan, follows hard rules (no medical advice) |
| **Persona** (simulator) | Plays a patient with a goal and traits, in ES or CA, switching language mid-call when the persona says so | ~8 | Stays in character, plays difficult cases without refusing, natural Catalan, cheap or free |
| **Judge** | Scores what code cannot check in each finished conversation | 1 | Agreement with hand labels, **in Catalan too**; different family from the receptionist |
| **Coach** | Reads failed conversations, proposes one prompt change with a reason | 1-2 per iteration, ~5 iterations per run | Diagnosis quality |
| **Embeddings** | Policy retrieval (`search_policies`): 20 chunks (10 policies × 2 languages) | 1 per question | ES/CA retrieval on colloquial questions |

STT/TTS are only researched if the voice stretch happens.

## Task per role

What each model receives, what it must return, and what rules it out. Anything that passes the filters is a candidate, whatever the provider.

### Receptionist

- **Input:** system prompt (clinic rules; this is the text the coach edits), the conversation so far, tool results. Patient text in ES or CA, colloquial, with typos, sometimes switching language mid-call; later it arrives from STT, without punctuation.
- **Tools it calls:** `find_patient`, `find_slots(service, from, to, staff?)`, `book`, `move`, `cancel`, `search_policies`. Arguments must be valid: service and staff ids from an enum, ISO dates resolved from "el dijous que ve" / "next Thursday" against today's date.
- **Output per turn:** either 1-3 short sentences to the patient, in the patient's language, or one or more tool calls. Prices, hours and policies only from tool results.
- **Hard requirements (filters):**
  - function calling with JSON schemas (enums, several calls per turn, multi-turn tool loops);
  - fluent ES **and** CA output, and replies in the patient's language when it switches;
  - reasoning can be turned off or set to minimum; full turn p95 ≤ ~2 s in text (≤ ~1 s to first token if voice happens);
  - system rules win over patient pressure: no medical advice, ask before any write (the graph enforces the write; the model must still ask the question);
  - input price ≤ ~$1 per 1M tokens;
  - an API our adapters speak (OpenAI-compatible or Anthropic), available until at least end of November.
- **Prompt layout rule (for caching):** the fixed part goes first (rules, clinic facts, tool definitions), the variable part last (today's date, patient). A prompt that changes at the top on every turn never hits the cache. Check the model's minimum cacheable length against our ~3k-token prompt.
- **Nice to have:** prompt caching, streaming, EU hosting.
- **Rules it out:** no tool calling; reasoning that cannot be switched off with slow first token; deprecated or announced end-of-life within the PoC.
- **Closest public proxy:** τ²-bench (customer service with tools and a policy to respect). English-only, and scores differ by split and site, so it only ranks candidates.

### Persona (simulated patient)

- **Input:** a persona card (goal, what the patient knows: name, phone, preferred days; traits: impatient, vague, elderly, rude; hidden constraints, e.g. "only Tuesdays after 17:00"; language behaviour, e.g. "starts in Spanish, switches to Catalan when annoyed") and the conversation so far.
- **Output:** the next patient message, short and natural, with the persona's register and mistakes; or an end marker (`[DONE]` when the goal is met, `[HANGUP]` when patience runs out).
- **Hard requirements:**
  - stays in character for 8-12 turns: no drift into a polite, helpful assistant (the literature's main finding: LLM simulators drift toward a verbose, cooperative style);
  - does not reveal what the card says to hold back, and does not invent facts outside the card;
  - **plays difficult cases without refusing:** a patient describing severe pain, a rude patient, one asking for a diagnosis. A model that refuses breaks exactly the emergency and medical-advice scenarios;
  - natural Catalan, including code-switching;
  - free or cheap (~500 calls per pass), and stable with a fixed temperature and seed.
- **Does not need:** tool calling, low latency.
- **Checked by code, not by the model:** refusals (patterns such as "como IA", "no puedo ayudar"; the conversation is marked as a **simulator failure** and left out of the score); card leaks (a field the card holds back appears before it is asked for); language per turn against the card. The persona prompt states that this is a simulation with synthetic data to test a receptionist, which lowers refusals on any model.
- **Family rule:** the persona runs in a different family from the receptionist under test, so the two do not "understand each other" better than a real patient would. The evidence for this effect is weak; the rule is cheap.
- **Note:** fidelity does not grow with model size (inverted U in one study), so a bigger model is not a safe default; it has to be read.

### Judge

- **Input:** the full transcript, the persona's success criteria, the tool-call log, the relevant policies, the rubric.
- **Output:** JSON, one object per criterion: `pass` (bool), `reason`, `evidence` (turn number and quote).
- **Split with code:** everything code can check is checked by code, not by the judge: the booking exists in the fake calendar with the right service, date and staff; no write happened without a confirmation turn; no price stated that is not in the clinic data. **The judge only gets what code cannot check:** policy explained correctly, no medical advice, language matches the patient's, tone, the patient was not left without an answer.
- **Hard requirements:** structured output (JSON schema); reads Catalan well; ~10k tokens of context; a different family from the receptionist under test; agreement with hand labels ≥ ~90% on the critical criteria (no medical advice, policy facts); low variance at temperature 0.
- **No batch inside the coach loop:** each iteration waits for the judge's scores to keep or roll back, and a batch can take hours. Batch only for reports that nobody waits on; costs below use standard prices.

### Coach

- **Input:** the current prompt, the change log so far (including rolled-back changes and why), a **summary of each failed visible conversation** (the judge's reason plus the relevant excerpt, ~20k tokens in total instead of ~75k of full transcripts), the rubric. Never held-out conversations.
- **Output:** exactly one change, structured: section, old text, new text, the failures it targets, the hypothesis, the risk (which passing personas might break).
- **Hard requirements:** strong reasoning over evidence; obeys "one change only" (does not rewrite the whole prompt); does not overfit to one transcript (a rule that names a specific patient is a red flag); structured output.
- **Does not need:** low latency.
- **Caching:** only the fixed prefix (instructions, rubric) repeats between iterations, and an iteration includes a simulator pass that can outlast the default 5-minute cache. Not counted in the budget until hits are measured.

### Embeddings

- **Input:** a patient question in ES or CA (colloquial, maybe from STT); corpus of 20 chunks (10 policies × 2 languages).
- **Output:** top-3 policy ids.
- **Hard requirements:** Catalan coverage; works on colloquial wording that shares no keywords with the policy ("me he retrasado" vs "late arrival"; "puc pagar a terminis?" vs "fraccionar"). Cross-lingual retrieval matters less, because every policy exists in both languages.
- **Does not need:** low cost (the corpus is tiny), an API (local is fine).

### Notes on the judge and Catalan

- **Judge:** binary criteria agree with humans far better than 1-5 scales (one report: kappa 0.74 on binary vs 0.41 on a 1-5 scale), so **each rubric item is a yes/no with a reason** ([koji.so](https://www.koji.so/docs/llm-as-a-judge-vs-human-evaluation)). Hand labels can correct a small judge's bias but not its ability to tell good from bad ([NeurIPS 2026](https://neurips.cc/virtual/2026/160516)). Judges lose accuracy outside English, by about 2 points in Catalan in one benchmark ([Galtea](https://galtea.ai/blog/multilingual-llm-judge-benchmark)), so **the calibration set must include Catalan conversations**.
- **Catalan:** no current benchmark ranks these models on Catalan ([IberBench](https://www.sciencedirect.com/science/article/abs/pii/S088523082500124X), [MuBench](https://aclanthology.org/2026.findings-acl.794.pdf) and [La Leaderboard](https://arxiv.org/html/2507.00999v1) exist but do not cover this shortlist with current scores). **Our own evals are the Catalan benchmark:** a language criterion in the judge, plus a manual read of ~10 Catalan conversations per candidate.

## Receptionist

Prices: USD per 1M tokens, standard tier.

| Model | Role in the test | Price in / out | What it must show | What rules it out |
|---|---|---|---|---|
| **Claude Haiku 5.5** (thinking off) | Main | $0.10 / $0.50 (prompts ≤100k) · cache read ~$0.01 | p95 ≤ 2 s per turn; no write without a confirmation question and no medical advice on the hard personas; correct Catalan in the manual read; cache hits > 0 | Slow first token with thinking off, or hard-rule failures |
| **Mistral Small 4** | EU lane | $0.15 / $0.60 · cache ~$0.015 | The same, with the gap to Haiku within the spread between repeats | Tool failures on the hard personas |
| **Gemini 3.5 Flash-Lite** | Reserve and voice | $0.30 / $2.50 | Keep W1's measured latency (p50 / p95 1.4 / 2.1 s, full answer with tools) with our tools | No latency edge over Haiku |
| **GPT-5.4 mini** | Quality ceiling, cascade | $0.75 / $4.50 · cache ~$0.075 | Fixes the small models' failures on the 5 hardest personas; if not, no cascade | — |

Notes:
- **Haiku 5.5:** effort defaults to `medium` (thinking on), so set it explicitly; thinking off is allowed at effort `high` or below. W1 saw 12.6 s to first token with thinking on (Anthropic model table, cached 2026-11-03; Claude prices in this file come from it). Its strength on hard rules is a hypothesis: not measured yet.
- **Mistral Small 4:** EU hosting **reduces** the regulatory risk for a real clinic (no international transfer); it does not remove it. Health data is a special category under GDPR (art. 9): a real deployment still needs a legal basis, a processor agreement (DPA) and an impact assessment. Its Catalan is unverified, and the only public signal is weak: 41.2% on Artificial Analysis' τ²-bench ([benchlm](https://benchlm.ai/benchmarks/tau2-bench)) vs 63.0% for GPT-5.4 mini on the airline split ([OpenRouter](https://openrouter.ai/benchmarks/tau2-bench-airline)) ([pricing](https://docs.mistral.ai/inference/pricing)).
- **Gemini 3.5 Flash-Lite:** native multimodal streaming only matters if the voice design changes; the plan is mic → STT → the same graph → TTS ([pricing](https://ai.google.dev/gemini-api/docs/pricing)).
- **GPT-5.4 mini** ([model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini)).

**Dropped, with the reason:**
- *Gemma 4 26B A4B, Qwen3.6-35B-A3B on Scaleway (Paris):* Mistral already covers the EU lane ([Scaleway models](https://www.scaleway.com/en/docs/generative-apis/reference-content/supported-models)).
- *DeepSeek V4.1 Flash:* China-hosted; adds nothing the shortlist lacks.
- *ALIA-40B fc (BSC):* the only Catalan-first model with tool calling, but its tool use is evaluated in English only and it is hosted by third parties with unknown pricing. Out of the $15 budget; moved to the backlog ([Friendli](https://friendli.ai/models/montevive/ALIA-40b-fc-2605-NVFP4), [EU case study](https://interoperable-europe.ec.europa.eu/collection/open-source-observatory-osor/document/case-study-alia)).
- *Groq / Cerebras:* only if the voice stretch happens and latency is the bottleneck ([costbench](https://costbench.com/best/fastest-llm-inference/)).
- *GPT-5.4 nano:* deprecated; OpenAI points to GPT-5.6 Luna ($0.20 / $1.20 on the pricing page, not verified on a model page).
- *Sonnet-class models:* too slow and expensive per turn until the small ones are shown to fail.

## Persona

| Model | Role in the test | Price in / out | What it must show | What rules it out |
|---|---|---|---|---|
| **Ministral 3 8B** | Cheap option | $0.15 / $0.15 (free tier to verify) | Stable character past turn 5; natural Catalan in the manual read | Drift into a polite assistant, or poor Catalan |
| **Current open model** (Gemma 4 or Qwen3.6, on a free tier such as Groq or Cerebras; to verify) | Fewer refusals | Free tier (to verify) | Passes the refusal probe; writes Catalan | No Catalan support, or not available |
| **Claude Haiku 5.5** | Card fidelity, only when the receptionist is not Claude | $0.10 / $0.50 | Passes the refusal probe; no card leaks | Refuses the emergency cases |

**Refusal probe:** every candidate plays an emergency with severe pain, a rude patient and a patient demanding a diagnosis.

**Check before choosing:** 3 personas × 2 runs per candidate, read by hand. Keep the persona model fixed across a coach run, or the score moves for reasons that have nothing to do with the prompt.

**Evidence:** no public benchmark names a best user simulator. The strongest model in SalesSim stays below 79% persona alignment and drifts toward the agent's suggestions; profile-conditioned simulators beat zero-shot ones ([SalesSim](https://www.alphaxiv.org/abs/2605.08334v1), [PT3 / RealUserSim](https://arxiv.org/html/2605.20204), [PICQ](https://arxiv.org/abs/2602.15832)). The persona card matters as much as the model.

**Dropped:** *Llama 3.3 70B* (Dec 2024): Catalan is not among its 8 officially supported languages, the price quoted ($0.59 / $0.79 on Groq) is from its launch and unverified today, and it would be the most expensive option in the role. The idea behind it (an open model refuses less) is kept and tested with a current open model.

## Judge

| Model | Role | Price in / out | What it must show | What rules it out |
|---|---|---|---|---|
| **Gemini 3.8 Flash** | Main judge (Haiku or Mistral receptionist) | $0.75 / $3.75 until 2026-12-31, then $1.50 / $7.50 · free tier to verify | ≥ 90% agreement with hand labels on the critical criteria, also on the Catalan subset | Low agreement in Catalan |
| **GPT-5.4 mini** | Second judge on the critical criteria; main judge when the receptionist is Gemini | $0.75 / $4.50 | The same; disagreements between the two judges go to a manual read | — |
| **Claude Sonnet 5.5** | Reference judge, only for non-Claude receptionists | $2 / $10 | Agreement that justifies ~2.5× the price | Claude receptionist |
| **Claude Haiku 5.5** | Cheap baseline, only for non-Claude receptionists | $0.10 / $0.50 | That a cheap judge is enough with binary criteria | Claude receptionist |

- **No judge is independent of every receptionist** (Claude, Mistral, Google and OpenAI all appear on the receptionist side), so the judge follows the receptionist under test. **Self-preference is measured, not assumed:** in calibration, compare each judge's agreement with the hand labels on conversations from its own family vs others. If there is no difference, the family rule is relaxed on data.
- Haiku is not the main judge because it shares the family of the main receptionist, not for lack of ability: after the split with code the judge answers yes/no on ~5 criteria, and the agreement numbers decide whether a cheap model is enough.
- Sources: Gemini 3.8 Flash intro price in the [release notes](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.8) (not yet on the main pricing page; batch pricing at the intro rate unverified). Cheap judges improve with several samples per item (GPT-5.4 mini with 8 samples: 79.2% on RewardBench 2) ([arXiv 2604.13717](https://arxiv.org/html/2604.13717v1)); not used here because it multiplies cost.

**Plan:** label ~20 conversations by hand (ES and CA, passes and failures), then measure agreement per criterion for Gemini 3.8 Flash and GPT-5.4 mini.

## Coach

| Model | Role | Price in / out | What it must show | What rules it out |
|---|---|---|---|---|
| **Claude Sonnet 5.5** | Main | $2 / $10 | Specific changes, justified by the conversations, that the keep rule keeps | Rewriting the whole prompt; rules that name one patient |
| **Claude Opus 5.5** | One-off comparison | $4 / $20 | Its changes clearly beat Sonnet's on the same failure batch | No clear gain over Sonnet |
| **Current DeepSeek or GPT-5.4 mini with reasoning** (to verify) | Cheap alternative | to verify | Matches Sonnet on the fixed batch | Does not match Sonnet |

- Opus: thinking always on; control it with effort (default `medium`).
- The coach's own calls are cheap (~$0.5-1 per run with summaries); the cost is in the simulator passes it triggers.
- **One-off comparison:** Sonnet, Opus and the cheap alternative each propose a change on the same fixed batch of failures (~$0.5-1.5 each). Judged on: specific, justified by the transcripts, kept by the keep rule.
- **Dropped:** *DeepSeek-R1* (Jan 2025, superseded by the V4.x line; its $0.55 / $2.19 price is from launch), *GPT-5.5* ($5 / $30, most expensive, no reason it would beat Opus).

## Embeddings

| Option | Role | Price | What it must show |
|---|---|---|---|
| **All policies in the prompt** (no retrieval) | Baseline zero | ~3k extra tokens per receptionist call, cached | The bar retrieval has to beat: if the receptionist answers policy questions as well this way, RAG is not earning its place (reported in the README either way) |
| **BM25** | Lexical baseline, used in CI | Free | Top-3 hit rate on the question set; needs ES and CA tokenisation and stemming (Catalan stemmer to verify) |
| **BGE-M3** | Local dense option | Free (CPU) | Beats BM25 on the colloquial questions with no shared keywords. Catalan is listed in its MTEB entry ([MTEB](https://mteb-leaderboard.hf.space/models/BAAI/bge-m3)). Heavy dependency (torch, ~2 GB download), so opt-in and never in CI |
| **text-embedding-3-small** | Hosted option | $0.02 per 1M tokens (fractions of a cent here) | The same as BGE-M3; uses the OpenAI key the project already needs ([model page](https://developers.openai.com/api/docs/models/text-embedding-3-small)) |

- **Question set:** 20 hand-written questions (10 ES, 10 CA), colloquial, many with no keyword overlap with the policy text. Metrics: top-1 and top-3 hit rate.
- **Decision rule, fixed beforehand:** if BM25 is within 1 question of the best embedding model, BM25 wins (simpler, no dependency).
- **Privacy:** the policies are public clinic information; what would leave the machine with a hosted model is the patient's question. Local BM25 or BGE-M3 keep it local.
- **Dropped:** *gemini-embedding* (covered by the two options above), *Qwen3-Embedding-0.6B* (Catalan not named).

## Cost per unit (estimate, to be replaced by measurements)

Assumptions: one conversation = 10 receptionist calls × ~3k input and ~200 output; 8 persona calls × ~1.5k / 100; 1 judge call × ~5k / 500.

| Setup | Receptionist | Persona | Judge | **Per conversation** | **Per pass** (20 personas × 3 repeats) |
|---|---|---|---|---|---|
| Free tiers for persona and judge: Haiku + free + free | $0.004 | $0 | $0 | **~$0.004** | **~$0.25** |
| All paid: Haiku + Ministral + Gemini 3.8 Flash | $0.004 | $0.002 | $0.006 | **~$0.012** | **~$0.70** |

## Budget ($15 cap)

| Item | Free tiers | All paid |
|---|---|---|
| Real smoke tests (5 conversations, a few times) | $0.3 | $0.5 |
| Judge calibration (20 conversations, 2-3 judges) | $0.2 | $0.7 |
| Receptionist comparison (3 models on the visible set × 2 repeats; GPT-5.4 mini on 5 hard personas) | $1.5 | $2.5 |
| Persona comparison (3 personas × 2 runs × 3 models) | $0 | $0.2 |
| Regression gate (baseline prompt and the bad change) | $0.5 | $1.5 |
| Coach (5 iterations, Sonnet; held-out run only when a change wins on visible) | $2-3 | $4-5 |
| One-off coach comparison | $1.5 | $1.5 |
| **Total** | **~$6-7** | **~$11-12** |

**How it stays under the cap:**
- All development runs on the scripted fake model (free). Real models only for passes that feed a decision.
- A cost ledger records the real cost of every call (`runs/cost.jsonl`). Before each run the runner prints the estimated cost and refuses to start if spent + estimate > $15; it warns at 80%.
- Spend limits set in each provider console that supports them.

**What the cap costs the experiment:**
1. **Statistical margin on done-when #2 (the main loss).** The held-out set is ~6 personas × 3 repeats = 18 conversations, so the coach's gain has to be large to beat the noise. Mitigation: start from a clearly weak prompt. **Best use of extra money:** a held-out set of ~10 personas, +$2-4, turns "plausible" into "defensible".
2. **One full coach run**, so no check that two runs agree. The one-off coach comparison stays.
3. **Free tiers are rate-limited:** a pass can take much longer, and a provider changing its quota stops us. Fallback: the all-paid column, still under $15.

## Constraints

- **Free tiers, to verify before D3:**
  - Gemini: ~500 requests per day for gemini-3.5-flash-lite in one forum report (2026-11-02, unverified; Google's docs give no fixed figure) ([forum](https://discuss.ai.google.dev/t/free-tier-daily-quota-not-reset-at-midnight-pacific-on-2-nov-2026-429-limit-500-after-one-request-gemini-3-5-flash-lite/197849)). Enough for the judge (~60 calls per pass), not for the persona (~500 per pass). Whether Gemini 3.8 Flash has a free tier is unverified.
  - Cerebras: 1M tokens per day reported by a third party ([morphllm](https://www.morphllm.com/cerebras-pricing)). Groq and Mistral free tiers: to check.
  - Free tiers may use the data for training: acceptable here only because all data is synthetic.
- **Data residency:** all data is synthetic, so any provider is fine for the PoC. The README must say which choices would change for a real clinic: EU hosting (Mistral, or Vertex/Bedrock EU regions), no China-hosted APIs, and the GDPR art. 9 obligations above.
- **Rate limits:** the simulator runs conversations in parallel. The `LLMClient` semaphore (from W1) has to be set per provider, lower on free tiers.
- **Keys:** every project has its own API keys (Pablo, 2026-11-11), so none are reused from W1: Anthropic, OpenAI, Gemini and Mistral keys are new for this project, plus Groq or Cerebras if their free tier carries the persona. Separate keys also give a clean cost per project on each provider's dashboard.

## Open items

- [ ] Verify the free tiers (Gemini 3.8 Flash and Flash-Lite, Mistral, Groq, Cerebras): quota, models, terms.
- [ ] Verify Gemma 4 / Qwen3.6 Catalan support and availability on a free tier.
- [ ] Measure Haiku 5.5 latency with thinking off (TTFT, full turn) on the receptionist prompt; check its minimum cacheable prompt length.
- [ ] Verify the current DeepSeek model and price for the cheap coach alternative.
- [ ] Confirm Gemini 3.8 Flash on the main pricing page.
- [ ] Check a Catalan stemmer for BM25.
- [ ] Measure per-conversation tokens and replace the cost estimate.
