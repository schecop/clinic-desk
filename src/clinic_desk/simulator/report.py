"""The eval report of a pass, from saved conversations and verdicts: report.md and report.html.

`clinic-desk report RUN` writes both into `runs/sim/<run>/`. No model is called, so it is free
and can be re-made at any time. What it answers, top to bottom:

1. The score: criteria passed (judge + lint), next to code pass, cost and latency, for the
   visible and held-out personas apart (the coach sees only the visible ones).
2. Which criteria fail, and how many of those failures the lint found.
3. Which personas fail, and how much a persona's score moves between repeats (the spread a
   change must beat before the coach keeps it).
4. Every failure with its evidence (turn and quote) and the trace link: what a person or the
   coach reads to decide the next prompt change.

`analyse` computes everything once; `to_markdown` and `to_html` only lay it out. The HTML is one
self-contained file (inline CSS and a small inline script for the section links and
failure filters; no network), light and dark.
"""

from __future__ import annotations

import html
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from clinic_desk.simulator.calibration import group
from clinic_desk.simulator.judge import Verdict
from clinic_desk.simulator.persona import Persona
from clinic_desk.simulator.runner import Conversation

TraceUrl = Callable[[str], str] | None


@dataclass(frozen=True)
class Score:
    passed: int
    applied: int

    @property
    def rate(self) -> float | None:
        return self.passed / self.applied if self.applied else None

    def __str__(self) -> str:
        r = self.rate
        return "-" if r is None else f"{r:.0%} ({self.passed}/{self.applied})"


def criteria_score(verdicts: list[Verdict]) -> Score:
    """Criteria passed / criteria that applied, over the verdicts without an error."""
    applied = [cv for v in verdicts if not v.error for cv in v.criteria if cv.applies]
    return Score(sum(cv.passed for cv in applied), len(applied))


@dataclass
class SplitRow:
    split: str
    conversations: int
    criteria: Score
    code_pass: int


@dataclass
class CriterionRow:
    id: str
    score: Score
    by_lint: int

    @property
    def failed(self) -> int:
        return self.score.applied - self.score.passed


@dataclass
class Link:
    label: str  # "r0"
    url: str | None


@dataclass
class PersonaRow:
    id: str
    split: str
    category: str
    rates: list[float]
    code_pass: int
    repeats: int
    failing: dict[str, int]
    traces: list[Link]

    @property
    def mean(self) -> float | None:
        return statistics.fmean(self.rates) if self.rates else None

    @property
    def spread(self) -> float | None:
        return max(self.rates) - min(self.rates) if len(self.rates) > 1 else None


@dataclass
class Failure:
    who: str  # "aina-late-cancel r0"
    trace: Link
    evidence: str
    reason: str  # empty for a lint hit (the evidence says it all)
    persona: str = ""  # for the HTML filters


@dataclass
class FailureGroup:
    id: str  # criterion id, "code checks" or "judge errors"
    title: str  # what the criterion asks (the card line for card_N)
    items: list[Failure] = field(default_factory=list)


@dataclass
class ReportData:
    run_id: str
    model: str
    splits: list[SplitRow]
    cost: Decimal
    per_conversation: Decimal
    p50: float
    p95: float
    not_counted: int
    judge_errors: int
    criteria: list[CriterionRow]
    personas: list[PersonaRow]
    failures: list[FailureGroup]


def _name(run_id: str, c: Conversation) -> str:
    return f"{run_id}/{c.persona_id}.r{c.repeat}"


def _link(c: Conversation, trace_url: TraceUrl) -> Link:
    label = f"r{c.repeat}"
    if not c.trace_id:
        return Link(label, None)
    return Link(f"{label} · {c.trace_id[:8]}", trace_url(c.trace_id) if trace_url else None)


def _pct(values: list[float], q: int) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


RUBRIC_TITLES = {
    "language": "patient's language, no words from another one, no grammar errors",
    "format": "speech for a phone call: no lists, no repeated questions or information",
    "tone": "courteous, natural, no needless justifying, closes the call properly",
    "facts_right": "every fact stated matches the clinic data and the tools",
    "no_medical_advice": "no assessment of symptoms, no medicine advice",
    "answered": "every question got an answer or a clear handoff",
}


def analyse(
    run_id: str,
    conversations: list[Conversation],
    verdicts: dict[str, Verdict],
    split_of: dict[str, str],
    personas: dict[str, Persona],
    trace_url: TraceUrl = None,
) -> ReportData:
    counted = [c for c in conversations if c.counted]
    by_name = {_name(run_id, c): c for c in counted}
    judged = {n: v for n, v in verdicts.items() if n in by_name}
    model = next(iter(judged.values())).model if judged else "none"
    cost = sum((c.usd for c in conversations), Decimal(0))
    seconds = [t.seconds for c in conversations for t in c.turns if t.desk]

    splits = []
    labels = sorted({split_of.get(c.persona_id, "-") for c in counted})
    for label in [*labels, *(["all"] if len(labels) > 1 else [])]:
        cs = [c for c in counted if label == "all" or split_of.get(c.persona_id, "-") == label]
        vs = [judged[_name(run_id, c)] for c in cs if _name(run_id, c) in judged]
        splits.append(SplitRow(label, len(cs), criteria_score(vs), sum(c.code_pass for c in cs)))

    per: dict[str, list[tuple[bool, bool]]] = defaultdict(list)  # (passed, failed by the lint)
    for v in judged.values():
        for cv in v.criteria if not v.error else []:
            if cv.applies:
                per[group(cv.id)].append((cv.passed, cv.reason.startswith("lint")))
    criteria = [
        CriterionRow(
            cid,
            Score(sum(ok for ok, _ in xs), len(xs)),
            sum(1 for ok, by_lint in xs if not ok and by_lint),
        )
        for cid, xs in per.items()
    ]
    criteria.sort(key=lambda r: (r.score.rate if r.score.rate is not None else 2.0, r.id))

    grouped: dict[str, list[Conversation]] = defaultdict(list)
    for c in counted:
        grouped[c.persona_id].append(c)
    rows = []
    for pid, cs in grouped.items():
        cs.sort(key=lambda c: c.repeat)
        rates: list[float] = []
        failing: dict[str, int] = defaultdict(int)
        for c in cs:
            found = judged.get(_name(run_id, c))
            if found is None or found.error:
                continue
            r = criteria_score([found]).rate
            if r is not None:
                rates.append(r)
            for cv in found.criteria:
                if cv.applies and not cv.passed:
                    failing[group(cv.id)] += 1
        rows.append(
            PersonaRow(
                pid,
                split_of.get(pid, "-"),
                cs[0].category,
                rates,
                sum(c.code_pass for c in cs),
                len(cs),
                dict(sorted(failing.items())),
                [_link(c, trace_url) for c in cs],
            )
        )
    rows.sort(key=lambda r: (r.mean if r.mean is not None else 2.0, r.id))

    failures: dict[str, FailureGroup] = {}

    def bucket(key: str, title: str) -> FailureGroup:
        return failures.setdefault(key, FailureGroup(key, title))

    for c in counted:
        if not c.code_pass:
            bucket("code checks", "calendar, false claims, language, end of call").items.append(
                Failure(
                    f"{c.persona_id} r{c.repeat}",
                    _link(c, trace_url),
                    f"end {c.end}; calendar {c.calendar_ok} ({c.calendar_why});"
                    f" false claims {c.false_claims}; language misses {c.language_misses}",
                    "",
                    c.persona_id,
                )
            )
    for name, v in sorted(judged.items()):
        c = by_name[name]
        who, link = f"{c.persona_id} r{c.repeat}", _link(c, trace_url)
        if v.error:
            bucket("judge errors", "the judge gave no usable verdict").items.append(
                Failure(who, link, v.error[:300], "", c.persona_id)
            )
            continue
        card = personas[c.persona_id].judge if c.persona_id in personas else []
        for cv in v.criteria:
            if not cv.applies or cv.passed:
                continue
            if cv.id.startswith("card_"):
                n = int(cv.id.split("_")[1])
                line = card[n - 1] if 0 < n <= len(card) else cv.id
                g, extra = bucket("card", "the persona card's own lines"), f"{cv.id}: {line}"
            else:
                g, extra = bucket(cv.id, RUBRIC_TITLES.get(cv.id, "")), ""
            lint = cv.reason.startswith("lint")
            evidence = cv.evidence.replace("\n", " ") or cv.reason
            g.items.append(
                Failure(
                    who + (f" · {extra}" if extra else ""),
                    link,
                    evidence,
                    "" if lint else cv.reason,
                    c.persona_id,
                )
            )
    order = ["code checks", *(r.id for r in criteria), "judge errors"]
    return ReportData(
        run_id=run_id,
        model=model,
        splits=splits,
        cost=cost,
        per_conversation=cost / max(len(conversations), 1),
        p50=_pct(seconds, 50),
        p95=_pct(seconds, 95),
        not_counted=len(conversations) - len(counted),
        judge_errors=sum(1 for v in judged.values() if v.error),
        criteria=criteria,
        personas=rows,
        failures=[failures[k] for k in order if k in failures],
    )


# --- markdown ------------------------------------------------------------------------------


def _md_link(link: Link) -> str:
    return f"[{link.label}]({link.url})" if link.url else link.label


def _pc(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def to_markdown(d: ReportData) -> str:
    lines = [
        f"# Eval report · {d.run_id}",
        "",
        f"Judge: {d.model}, with the code lint. Criteria passed counts every criterion that"
        " applied (card lines and the rubric), judge and lint together.",
        "",
        "| personas | conversations | criteria passed | code pass |",
        "|---|---|---|---|",
        *(
            f"| {s.split} | {s.conversations} | **{s.criteria}** |"
            f" {s.code_pass}/{s.conversations} |"
            for s in d.splits
        ),
        "",
        f"- Cost: ${d.cost:.4f} for the pass, ${d.per_conversation:.4f} per conversation"
        " (receptionist, persona and translator; the judge is in the ledger)",
        f"- Receptionist latency per turn: p50 {d.p50:.1f}s · p95 {d.p95:.1f}s",
        f"- Not counted (simulator errors): {d.not_counted} · judge errors: {d.judge_errors}",
        "",
        "## By criterion",
        "",
        "| criterion | passed | failed | found by the lint |",
        "|---|---|---|---|",
        *(f"| {r.id} | {r.score} | {r.failed} | {r.by_lint} |" for r in d.criteria),
        "",
        "## By persona",
        "",
        "Spread: highest minus lowest criteria score across repeats. A prompt change has to"
        " beat it to count as a real gain.",
        "",
        "| persona | split | category | per repeat | mean | spread | code pass | failing |"
        " traces |",
        "|---|---|---|---|---|---|---|---|---|",
        *(
            f"| {p.id} | {p.split} | {p.category} |"
            f" {' · '.join(_pc(x) for x in p.rates) or '-'} | {_pc(p.mean)} |"
            f" {_pc(p.spread)} | {p.code_pass}/{p.repeats} |"
            f" {', '.join(f'{k} {n}' for k, n in p.failing.items()) or '-'} |"
            f" {' '.join(_md_link(t) for t in p.traces)} |"
            for p in d.personas
        ),
        "",
        "## Failures",
        "",
    ]
    for g in d.failures:
        lines += [f"### {g.id} ({len(g.items)})", "", f"_{g.title}_", ""]
        lines += [
            f"- {f.who} · {_md_link(f.trace)}: {f.evidence}"
            + (f" — {f.reason}" if f.reason else "")
            for f in g.items
        ]
        lines.append("")
    if not d.criteria:
        lines.append("No verdicts for this pass: run `clinic-desk judge RUN` first.")
    return "\n".join(lines).rstrip() + "\n"


# --- html ----------------------------------------------------------------------------------

_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;
--good:#2f7d4f;--good-bg:#e3f2e8;--mid:#a86b00;--mid-bg:#fbefd9;--bad:#b3261e;--bad-bg:#fbe4e1;
--accent:#3456a8;--lint:#6a4bb0;--lint-bg:#ece6f8}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151514;--card:#1f1f1d;
--ink:#ecece8;--muted:#a3a39c;--line:#33332f;--good:#6cc58f;--good-bg:#1d3326;--mid:#e0a64a;
--mid-bg:#3a2c14;--bad:#f08a80;--bad-bg:#3d1d1a;--accent:#8fa8ec;--lint:#b49cf0;--lint-bg:#2c2440}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,
"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.sub{color:var(--muted);margin:0 0 20px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:24px;font-variant-numeric:tabular-nums}
.kpi span{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 0;
overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:0}
.bar{position:relative;height:8px;border-radius:4px;background:var(--line);min-width:120px}
.bar i{position:absolute;inset:0 auto 0 0;border-radius:4px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12.5px;
margin:1px 2px 1px 0;white-space:nowrap}
.good{color:var(--good);background:var(--good-bg)}.mid{color:var(--mid);background:var(--mid-bg)}
.bad{color:var(--bad);background:var(--bad-bg)}.lint{color:var(--lint);background:var(--lint-bg)}
.muted{color:var(--muted)}a{color:var(--accent)}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0}
summary{cursor:pointer;padding:10px 14px;font-weight:600}
summary .muted{font-weight:400}
ul{margin:0;padding:0 14px 10px 32px}li{margin:6px 0}
.ev{background:var(--bg);border-radius:6px;padding:2px 6px}
"""


def _tone(rate: float | None) -> str:
    if rate is None:
        return "muted"
    return "good" if rate >= 0.85 else ("mid" if rate >= 0.6 else "bad")


def _bar(rate: float | None) -> str:
    if rate is None:
        return '<span class="muted">-</span>'
    color = {"good": "var(--good)", "mid": "var(--mid)", "bad": "var(--bad)"}[_tone(rate)]
    return f'<div class="bar"><i style="width:{rate * 100:.0f}%;background:{color}"></i></div>'


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{html.escape(text)}</span>'


def _a(link: Link) -> str:
    if not link.url:
        return html.escape(link.label)
    url, label = html.escape(link.url), html.escape(link.label)
    return f'<a href="{url}" target="_blank" rel="noopener">{label}</a>'


_CSS_NAV = """
nav{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
padding:8px 0;margin:0 0 16px;display:flex;gap:6px;flex-wrap:wrap}
nav a{text-decoration:none;color:var(--ink);padding:4px 10px;border-radius:999px;font-size:14px}
nav a:hover,nav a.on{background:var(--card);box-shadow:0 0 0 1px var(--line)}
section{scroll-margin-top:56px}
[data-crit],[data-persona]{cursor:pointer}
tr[data-crit]:hover td,tr[data-persona]:hover td{background:var(--bg)}
.pill[data-crit]:hover,.who[data-persona]:hover{outline:1px solid currentColor}
.who{border-radius:4px;padding:0 2px}
.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 8px}
.filters button{font:inherit;font-size:13px;background:var(--card);color:var(--ink);
border:1px solid var(--line);border-radius:999px;padding:3px 10px;cursor:pointer}
.filters button:hover{border-color:var(--accent)}
.chip{background:var(--accent);color:var(--bg);border-radius:999px;padding:2px 10px;font-size:13px}
.hide{display:none}
.hint{font-size:13px;color:var(--muted)}
"""

_JS = """
(() => {
  const f = {crit: null, persona: null};
  const $ = (s, r = document) => [...r.querySelectorAll(s)];
  const failures = document.getElementById('failures');
  function apply(scroll) {
    let shown = 0;
    $('#failures details').forEach(d => {
      const crit = d.dataset.group;
      let n = 0;
      $('li', d).forEach(li => {
        const ok = (!f.crit || crit === f.crit) && (!f.persona || li.dataset.p === f.persona);
        li.classList.toggle('hide', !ok);
        if (ok) n++;
      });
      d.classList.toggle('hide', n === 0);
      d.querySelector('.count').textContent = n;
      if (f.crit || f.persona) d.open = n > 0;
      shown += n;
    });
    const bar = document.getElementById('active');
    const chips = [];
    if (f.crit) chips.push('criterion: ' + f.crit);
    if (f.persona) chips.push('persona: ' + f.persona);
    bar.innerHTML = chips.length
      ? chips.map(c => '<span class="chip"></span>').join(' ')
      : '<span class="hint">Click a criterion, a persona or a failing tag to filter.</span>';
    $('.chip', bar).forEach((el, i) => el.textContent = chips[i]);
    document.getElementById('clear').classList.toggle('hide', !chips.length);
    document.getElementById('shown').textContent = shown + ' shown';
    if (scroll) failures.scrollIntoView({behavior: 'smooth'});
  }
  document.addEventListener('click', ev => {
    const el = ev.target.closest('[data-crit],[data-persona]');
    if (!el || ev.target.closest('a')) return;
    f.crit = el.dataset.crit ?? (el.dataset.persona && el.dataset.keep ? f.crit : null);
    f.persona = el.dataset.persona ?? null;
    apply(true);
  });
  document.getElementById('clear').onclick = () => { f.crit = f.persona = null; apply(false); };
  const openAll = open => $('#failures details:not(.hide)').forEach(d => d.open = open);
  document.getElementById('open-all').onclick = () => openAll(true);
  document.getElementById('close-all').onclick = () => openAll(false);
  const links = $('nav a');
  const io = new IntersectionObserver(es => es.forEach(e => {
    if (!e.isIntersecting) return;
    links.forEach(a => a.classList.toggle('on', a.hash === '#' + e.target.id));
  }), {rootMargin: '-40% 0px -55% 0px'});
  $('section').forEach(s => io.observe(s));
  apply(false);
})();
"""


def to_html(d: ReportData) -> str:
    e = html.escape
    main = next((s for s in d.splits if s.split == "visible"), d.splits[0] if d.splits else None)
    failing_groups = {g.id for g in d.failures}
    kpis = []
    for s in d.splits:
        kpis.append(
            f'<div class="kpi"><span>criteria passed · {e(s.split)}</span>'
            f"<b>{_pc(s.criteria.rate)}</b><span>{s.criteria.passed} of {s.criteria.applied}"
            f" · code pass {s.code_pass}/{s.conversations}</span></div>"
        )
    kpis += [
        f'<div class="kpi"><span>cost of the pass</span><b>${d.cost:.2f}</b>'
        f"<span>${d.per_conversation:.4f} per conversation</span></div>",
        f'<div class="kpi"><span>latency per turn</span><b>{d.p50:.1f}s</b>'
        f"<span>p50 · p95 {d.p95:.1f}s</span></div>",
    ]

    def crit_attr(cid: str) -> str:
        return f' data-crit="{e(cid)}"' if cid in failing_groups else ""

    crit_rows = "".join(
        f"<tr{crit_attr(r.id)}><td><b>{e(r.id)}</b><br>"
        f"<span class='muted'>{e(RUBRIC_TITLES.get(r.id, ''))}</span></td>"
        f"<td>{_bar(r.score.rate)}</td>"
        f"<td>{_pill(_pc(r.score.rate), _tone(r.score.rate))}"
        f" <span class='muted'>{r.score.passed}/{r.score.applied}</span></td>"
        f"<td>{r.failed}</td><td>{_pill(str(r.by_lint), 'lint') if r.by_lint else '0'}</td></tr>"
        for r in d.criteria
    )

    def fail_tag(pid: str, cid: str, n: int) -> str:
        return (
            f'<span class="pill bad" data-crit="{e(cid)}" data-persona="{e(pid)}"'
            f' title="failures of {e(pid)} on {e(cid)}">{e(cid)} {n}</span>'
        )

    persona_rows = "".join(
        f'<tr data-persona="{e(p.id)}"><td><b>{e(p.id)}</b><br>'
        f"<span class='muted'>{e(p.category)} · {e(p.split)}</span></td>"
        f"<td>{''.join(_pill(_pc(x), _tone(x)) for x in p.rates) or '-'}</td>"
        f"<td>{_bar(p.mean)}<span class='muted'>{_pc(p.mean)}</span></td>"
        f"<td>{_pc(p.spread)}</td><td>{p.code_pass}/{p.repeats}</td>"
        f"<td>{''.join(fail_tag(p.id, k, n) for k, n in p.failing.items()) or '-'}</td>"
        f"<td>{'<br>'.join(_a(t) for t in p.traces)}</td></tr>"
        for p in d.personas
    )
    groups = "".join(
        f'<details data-group="{e(g.id)}"{" open" if i == 0 else ""}><summary>{e(g.id)} ·'
        f' <span class="count">{len(g.items)}</span>'
        f" <span class='muted'>— {e(g.title)}</span></summary><ul>"
        + "".join(
            f'<li data-p="{e(f.persona)}"><b class="who" data-persona="{e(f.persona)}"'
            f' data-keep="1" title="only {e(f.persona)}">{e(f.who)}</b> · {_a(f.trace)}<br>'
            f"<span class='ev'>{e(f.evidence)}</span>"
            + (f"<br><span class='muted'>{e(f.reason)}</span>" if f.reason else "")
            + "</li>"
            for f in g.items
        )
        + "</ul></details>"
        for i, g in enumerate(d.failures)
    )
    headline = _pc(main.criteria.rate) if main else "-"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Eval report {e(d.run_id)}</title><style>{_CSS}{_CSS_NAV}</style></head>
<body><main>
<nav><a href="#summary">Summary</a><a href="#criteria">By criterion</a>
<a href="#personas">By persona</a><a href="#failures">Failures</a></nav>
<section id="summary">
<h1>Eval report · {e(d.run_id)}</h1>
<p class="sub">Criteria passed {headline} · judge {e(d.model)} with the code lint ·
{d.not_counted} not counted · {d.judge_errors} judge errors</p>
<div class="kpis">{"".join(kpis)}</div>
</section>
<section id="criteria">
<h2>By criterion</h2>
<p class="sub">Click a criterion to see its failures.</p>
<div class="card"><table><tr><th>criterion</th><th>pass rate</th><th>passed</th>
<th>failed</th><th>found by the lint</th></tr>{crit_rows}</table></div>
</section>
<section id="personas">
<h2>By persona</h2>
<p class="sub">Worst first. Click a persona for its failures, or a red tag for one criterion.
Spread: highest minus lowest score across repeats; a prompt change has to beat it to count as
a real gain.</p>
<div class="card"><table><tr><th>persona</th><th>per repeat</th><th>mean</th><th>spread</th>
<th>code pass</th><th>failing</th><th>traces</th></tr>{persona_rows}</table></div>
</section>
<section id="failures">
<h2>Failures</h2>
<div class="filters"><span id="active"></span><button id="clear" class="hide">clear filter
</button><span class="hint" id="shown"></span><span style="flex:1"></span>
<button id="open-all">open all</button><button id="close-all">close all</button></div>
{groups or '<p class="muted">None.</p>'}
</section>
</main><script>{_JS}</script></body></html>
"""
