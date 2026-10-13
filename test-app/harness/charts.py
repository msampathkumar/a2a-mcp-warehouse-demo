"""Statistics + SVG charts for a sweep. No plotting dependency: plain SVG.

    python -m harness.charts v2        # -> docs/charts/*.svg, docs/results/stats.md

All values come from report.per_attempt over the sweep's raw evidence.
Error bars are 95% confidence intervals of the mean (1.96 · sd / √n).
"""

from __future__ import annotations

import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

from .report import load, per_attempt
from .summary import latest_batches

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT.parent / 'docs'
CHARTS = DOCS / 'charts'
MCP_C, A2A_C = '#d93025', '#1a73e8'
W, H, L, R, T, B = 520, 320, 62, 16, 40, 62


def _ci(xs):
    return 1.96 * st.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0.0


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))]


def _fmt(v):
    if v >= 1_000_000:
        return f'{v / 1e6:.1f}M'
    if v >= 1000:
        return f'{v / 1000:.0f}k'
    return f'{v:g}'


def _nice(top):
    """Round the axis top up to 1/2/5 x 10^k so tick labels are clean."""
    raw = top / 4
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return step * 4


def _frame(title, ymax, ylabel, xlabels, log=False, ymin=1.0):
    def y(v):
        if log:
            v = max(v, ymin)
            return T + (H - T - B) * (
                1
                - (math.log10(v) - math.log10(ymin))
                / (math.log10(ymax) - math.log10(ymin))
            )
        return T + (H - T - B) * (1 - v / ymax)

    def x(i):
        return L + (W - L - R) * (i + 0.5) / len(xlabels)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="Arial,sans-serif" font-size="11">',
        f'<rect width="{W}" height="{H}" fill="#fff"/>',
        f'<text x="{W / 2}" y="20" text-anchor="middle" font-size="14" font-weight="bold" fill="#202124">{title}</text>',
    ]
    ticks = (
        [ymin * 10**k for k in range(0, int(math.log10(ymax / ymin)) + 1)]
        if log
        else [ymax * k / 4 for k in range(5)]
    )
    for t in ticks:
        out.append(
            f'<line x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="#e8eaed"/>'
        )
        out.append(
            f'<text x="{L - 6}" y="{y(t) + 4:.1f}" text-anchor="end" fill="#5f6368">{_fmt(t)}</text>'
        )
    for i, lab in enumerate(xlabels):
        out.append(
            f'<text x="{x(i):.1f}" y="{H - B + 16}" text-anchor="middle" fill="#5f6368">{lab}</text>'
        )
    out.append(
        f'<text x="14" y="{(T + H - B) / 2}" transform="rotate(-90 14 {(T + H - B) / 2})" text-anchor="middle" fill="#5f6368">{ylabel}</text>'
    )
    return out, x, y


def _legend(out, items):
    xx = L + 4
    for label, color in items:
        out.append(
            f'<rect x="{xx}" y="{H - 16}" width="10" height="10" fill="{color}"/>'
        )
        out.append(
            f'<text x="{xx + 14}" y="{H - 7}" fill="#202124">{label}</text>'
        )
        xx += 14 + 7 * len(label) + 16


def line_chart(
    name,
    title,
    xlabels,
    series,
    ylabel,
    log=False,
    xtitle='Sources needed per question',
):
    """series: [(label, color, means, cis)]"""
    top = max(m + c for _, _, ms, cs in series for m, c in zip(ms, cs))
    ymax = 10 ** math.ceil(math.log10(top)) if log else _nice(top * 1.05)
    out, x, y = _frame(title, ymax, ylabel, xlabels, log=log)
    out.append(
        f'<text x="{(L + W - R) / 2}" y="{H - B + 32}" text-anchor="middle" fill="#5f6368">{xtitle}</text>'
    )
    for _, color, ms, cs in series:
        pts = ' '.join(f'{x(i):.1f},{y(m):.1f}' for i, m in enumerate(ms))
        out.append(
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5"/>'
        )
        for i, (m, c) in enumerate(zip(ms, cs)):
            if c:
                out.append(
                    f'<line x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{y(m + c):.1f}" y2="{y(max(m - c, 1e-9 if not log else 1)):.1f}" stroke="{color}" stroke-opacity=".5"/>'
                )
            out.append(
                f'<circle cx="{x(i):.1f}" cy="{y(m):.1f}" r="3.5" fill="{color}"/>'
            )
    _legend(out, [(lab, col) for lab, col, _, _ in series])
    _write(name, out)


def bar_chart(name, title, xlabels, series, ylabel, stacked_on=None, xtitle=''):
    """series: [(label, color, values)]; grouped bars. stacked_on: {series_idx: (label, color, values)} adds a stacked segment."""
    tops = [v for _, _, vs in series for v in vs]
    if stacked_on:
        for idx, (_, _, vs) in stacked_on.items():
            tops += [a + b for a, b in zip(series[idx][2], vs)]
    ymax = 100 if '%' in ylabel else _nice(max(tops) * 1.08)
    out, x, y = _frame(title, ymax, ylabel, xlabels)
    if xtitle:
        out.append(
            f'<text x="{(L + W - R) / 2}" y="{H - B + 32}" text-anchor="middle" fill="#5f6368">{xtitle}</text>'
        )
    slot = (W - L - R) / len(xlabels)
    bw = slot * 0.7 / len(series)
    for si, (_, color, vs) in enumerate(series):
        for i, v in enumerate(vs):
            bx = x(i) - slot * 0.35 + si * bw
            out.append(
                f'<rect x="{bx:.1f}" y="{y(v):.1f}" width="{bw - 2:.1f}" height="{y(0) - y(v):.1f}" fill="{color}"/>'
            )
            label_v = v
            if stacked_on and si in stacked_on:
                extra = stacked_on[si][2][i]
                out.append(
                    f'<rect x="{bx:.1f}" y="{y(v + extra):.1f}" width="{bw - 2:.1f}" height="{y(v) - y(v + extra):.1f}" fill="{stacked_on[si][1]}"/>'
                )
                label_v = v + extra
            out.append(
                f'<text x="{bx + bw / 2 - 1:.1f}" y="{y(label_v) - 3:.1f}" text-anchor="middle" font-size="9" fill="#202124">{_fmt(round(label_v, 1))}</text>'
            )
    items = [(lab, col) for lab, col, _ in series]
    if stacked_on:
        items += [(lab, col) for lab, col, _ in stacked_on.values()]
    _legend(out, items)
    _write(name, out)


def _write(name, out):
    CHARTS.mkdir(parents=True, exist_ok=True)
    (CHARTS / f'{name}.svg').write_text('\n'.join(out + ['</svg>']) + '\n')


def main(tag: str) -> None:
    rows = []
    for dirs in latest_batches(tag).values():
        for d in dirs:
            _, results, client, server = load(d)
            rows += per_attempt(results, client, server)

    g = defaultdict(list)
    for r in rows:
        g[(len(r['sources_needed']), r['path'])].append(r)
    srcs = sorted({k[0] for k in g})
    xl = [str(s) for s in srcs]

    def col(path, f):
        return [[f(r) for r in g[(s, path)]] for s in srcs]

    def ms(vals):
        return [st.mean(v) for v in vals], [_ci(v) for v in vals]

    calls_m = ms(col('mcp', lambda r: r['mcp_calls']))
    calls_a = ms(col('a2a', lambda r: r['a2a_calls']))
    dm_m = ms(col('mcp', lambda r: r['dm_calls_mcp']))
    dm_a = ms(col('a2a', lambda r: r['dm_calls_a2a']))
    tok_m = ms(col('mcp', lambda r: r['client_tokens']))
    tok_a = ms(col('a2a', lambda r: r['client_tokens']))
    tot_a_client = [
        st.mean(v) for v in col('a2a', lambda r: r['client_tokens'])
    ]
    tot_a_remote = [
        st.mean(v) for v in col('a2a', lambda r: r['remote_tokens'])
    ]
    sec_m = col('mcp', lambda r: r['elapsed_ms'] / 1000)
    sec_a = col('a2a', lambda r: r['elapsed_ms'] / 1000)
    acc_m = [100 * st.mean(v) for v in col('mcp', lambda r: r['correct'])]
    acc_a = [100 * st.mean(v) for v in col('a2a', lambda r: r['correct'])]

    line_chart(
        '01-user-calls',
        "Calls from the user's agent per question",
        xl,
        [('MCP', MCP_C, *calls_m), ('A2A', A2A_C, *calls_a)],
        'calls (mean, 95% CI)',
    )
    line_chart(
        '02-user-tokens',
        "Tokens in the user's agent per question (log)",
        xl,
        [('MCP', MCP_C, *tok_m), ('A2A', A2A_C, *tok_a)],
        'tokens (log scale)',
        log=True,
    )
    line_chart(
        '03-source-calls',
        'Calls to the data sources per question',
        xl,
        [('MCP', MCP_C, *dm_m), ('A2A', A2A_C, *dm_a)],
        'DataManager calls',
    )
    bar_chart(
        '04-latency',
        'End-to-end seconds per question: median and p90',
        xl,
        [
            ('MCP p50', '#f28b82', [_pct(v, 0.5) for v in sec_m]),
            ('MCP p90', MCP_C, [_pct(v, 0.9) for v in sec_m]),
            ('A2A p50', '#8ab4f8', [_pct(v, 0.5) for v in sec_a]),
            ('A2A p90', A2A_C, [_pct(v, 0.9) for v in sec_a]),
        ],
        'seconds',
        xtitle='Sources needed per question',
    )
    bar_chart(
        '05-accuracy',
        'Correct answers by sources needed (%)',
        xl,
        [('MCP', MCP_C, acc_m), ('A2A', A2A_C, acc_a)],
        '% correct',
        xtitle='Sources needed per question',
    )
    bar_chart(
        '06-total-tokens',
        'Total tokens per question (who pays)',
        xl,
        [
            ("MCP: user's agent", MCP_C, tok_m[0]),
            ("A2A: user's agent", A2A_C, tot_a_client),
        ],
        'tokens',
        stacked_on={1: ('A2A: remote agent', '#a8c7fa', tot_a_remote)},
        xtitle='Sources needed per question',
    )

    tot_m = tok_m[0]
    tot_a = [c + r for c, r in zip(tot_a_client, tot_a_remote)]
    print('crossover ratios', [round(v, 2) for v in crossover_chart(srcs, tot_m, tot_a)])

    # --- 10-second "versus" charts: MCP and A2A bars side by side ---
    lv = sorted({r["level"] for r in rows})
    gl = defaultdict(list)
    for r in rows:
        gl[(r["level"], r["path"])].append(r)

    def lvl(path, f):
        return [st.mean(f(r) for r in gl[(L_, path)]) for L_ in lv]

    tot = lambda r: r["client_tokens"] + r["remote_tokens"]
    llm = lambda r: r["client_llm_calls"] + r["remote_llm_calls"]
    sx = "Data sources needed per question"
    versus_chart("10-vs-tokens-by-sources", "Fewer sources: MCP is cheaper. More sources: A2A is.",
                 "Total tokens per question, both agents (lower is better)", xl,
                 tot_m, tot_a, "tokens", xtitle=sx)
    versus_chart("11-vs-tokens-by-level", "Levels 1-3: MCP is cheaper. Levels 4-5: A2A is.",
                 "Total tokens per question, both agents (lower is better)", [f"L{l} ({l} tbl)" for l in lv],
                 lvl("mcp", tot), lvl("a2a", tot), "tokens", xtitle="Level (tables available)")
    versus_chart("12-vs-llm-calls-by-sources", "Model calls grow faster on MCP as sources grow",
                 "LLM calls per question, both agents (lower is better)", xl,
                 [st.mean(llm(r) for r in g[(s_, "mcp")]) for s_ in srcs],
                 [st.mean(llm(r) for r in g[(s_, "a2a")]) for s_ in srcs], "LLM calls", xtitle=sx)
    versus_chart("13-vs-user-calls-by-sources", "Your agent: up to ~80 MCP calls vs 1 A2A message",
                 "Calls your agent makes per question (lower is better)", xl,
                 calls_m[0], calls_a[0], "calls", xtitle=sx)
    versus_chart("14-vs-accuracy-by-sources", "Accuracy: A2A holds up better at 2-4 sources",
                 "Correct answers (higher is better)", xl, acc_m, acc_a, "% correct",
                 lower_is_better=False, xtitle=sx, unit="%")

    # per-trial accuracy (spread across trials)
    tr = defaultdict(lambda: defaultdict(list))
    for r in rows:
        tr[r['path']][r['trial']].append(r['correct'])
    trials = sorted(tr['mcp'])
    acc_tr = {
        p: [100 * st.mean(tr[p][t]) for t in trials] for p in ('mcp', 'a2a')
    }
    bar_chart(
        '07-accuracy-per-trial',
        'Accuracy per trial, all 50 questions (%)',
        [f'trial {t}' for t in trials],
        [('MCP', MCP_C, acc_tr['mcp']), ('A2A', A2A_C, acc_tr['a2a'])],
        '% correct',
    )

    # playbook effect: v1 vs v2, A2A per question
    v1 = DOCS / 'results' / 'summary-v1.json'
    if v1.exists():
        rows_v1 = []
        for d in [
            p
            for p in (DOCS / 'results').iterdir()
            if p.is_dir()
            and '-v2-' not in p.name
            and (p / 'batch.json').exists()
        ]:
            _, results, client, server = load(d)
            rows_v1 += [
                r
                for r in per_attempt(results, client, server)
                if r['path'] == 'a2a'
            ]
        a2 = [r for r in rows if r['path'] == 'a2a']
        bar_chart(
            '08-playbook-effect',
            'A2A agent before and after the playbook (per question)',
            ['source calls', 'tokens / 1000'],
            [
                (
                    'no playbook (v1)',
                    '#9aa0a6',
                    [
                        st.mean(r['dm_calls_a2a'] for r in rows_v1),
                        st.mean(
                            r['client_tokens'] + r['remote_tokens']
                            for r in rows_v1
                        )
                        / 1000,
                    ],
                ),
                (
                    'playbook (v2)',
                    A2A_C,
                    [
                        st.mean(r['dm_calls_a2a'] for r in a2),
                        st.mean(
                            r['client_tokens'] + r['remote_tokens'] for r in a2
                        )
                        / 1000,
                    ],
                ),
            ],
            'per question',
        )

    # stats table
    lines = [
        f'# Statistics — sweep `{tag}`',
        '',
        'Mean ± 95% CI per attempt; p50/p90 of end-to-end seconds.',
        '',
        "| Sources | Path | n | Correct | Calls from user's agent | Source calls | Tokens (user's agent) | Tokens (total) | LLM calls (total) | p50 s | p90 s |",
        '|:---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for s in srcs:
        for p in ('mcp', 'a2a'):
            R = g[(s, p)]
            calls = [
                r['mcp_calls'] if p == 'mcp' else r['a2a_calls'] for r in R
            ]
            dm = [
                r['dm_calls_mcp'] if p == 'mcp' else r['dm_calls_a2a']
                for r in R
            ]
            tok = [r['client_tokens'] for r in R]
            tot = [r['client_tokens'] + r['remote_tokens'] for r in R]
            llm = [r['client_llm_calls'] + r['remote_llm_calls'] for r in R]
            sec = [r['elapsed_ms'] / 1000 for r in R]
            lines.append(
                f'| {s} | {p.upper()} | {len(R)} | {100 * st.mean(r["correct"] for r in R):.0f}% | {st.mean(calls):.1f} ± {_ci(calls):.1f} '
                f'| {st.mean(dm):.1f} ± {_ci(dm):.1f} | {st.mean(tok):,.0f} ± {_ci(tok):,.0f} | {st.mean(tot):,.0f} | {st.mean(llm):.1f} '
                f'| {_pct(sec, 0.5):.1f} | {_pct(sec, 0.9):.1f} |'
            )
    lines += [
        '',
        'Accuracy per trial (all 50 questions):',
        '',
        '| Path | ' + ' | '.join(f'Trial {t}' for t in trials) + ' |',
        '|---|' + '---:|' * len(trials),
    ]
    for p in ('mcp', 'a2a'):
        lines.append(
            f'| {p.upper()} | '
            + ' | '.join(f'{v:.0f}%' for v in acc_tr[p])
            + ' |'
        )
    (DOCS / 'results' / 'stats.md').write_text('\n'.join(lines) + '\n')
    print('\n'.join(sorted(str(p.name) for p in CHARTS.glob('*.svg'))))


def crossover_chart(srcs, mcp_tokens, a2a_tokens):
    """One simple picture: total tokens per question, A2A relative to MCP (MCP = 1.0).
    Above the line MCP costs less; below it A2A costs less."""
    ratios = [a / m for a, m in zip(a2a_tokens, mcp_tokens)]
    ymax = 1.6
    out, x, y = _frame("Which path costs less as data sources grow?", ymax, "A2A tokens ÷ MCP tokens", [str(s) for s in srcs])
    out.append(f'<text x="{(L + W - R) / 2}" y="{H - B + 32}" text-anchor="middle" fill="#5f6368">Data sources needed per question</text>')
    out.append(f'<rect x="{L}" y="{y(ymax):.1f}" width="{W - L - R}" height="{y(1) - y(ymax):.1f}" fill="#fce8e6" opacity=".6"/>')
    out.append(f'<rect x="{L}" y="{y(1):.1f}" width="{W - L - R}" height="{y(0) - y(1):.1f}" fill="#e8f0fe" opacity=".6"/>')
    out.append(f'<text x="{W - R - 6}" y="{y(ymax) + 16:.1f}" text-anchor="end" font-weight="bold" fill="{MCP_C}">MCP costs less</text>')
    out.append(f'<text x="{x(0):.1f}" y="{y(0.45):.1f}" text-anchor="middle" font-weight="bold" fill="{A2A_C}">A2A costs less</text>')
    out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(1):.1f}" y2="{y(1):.1f}" stroke="#202124" stroke-dasharray="5,4"/>')
    out.append(f'<text x="{W - R - 6}" y="{y(1) - 5:.1f}" text-anchor="end" fill="#202124">same cost</text>')
    slot = (W - L - R) / len(srcs)
    for i, r in enumerate(ratios):
        color = MCP_C if r > 1 else A2A_C
        bx = x(i) - slot * 0.22
        top, bottom = (y(r), y(1)) if r > 1 else (y(1), y(r))
        out.append(f'<rect x="{bx:.1f}" y="{top:.1f}" width="{slot * 0.44:.1f}" height="{bottom - top:.1f}" fill="{color}"/>')
        ly = y(r) - 5 if r > 1 else y(r) + 13
        out.append(f'<text x="{x(i):.1f}" y="{ly:.1f}" text-anchor="middle" font-weight="bold" fill="#202124">{r:.2f}×</text>')
    out.append(f'<text x="{L + 4}" y="{H - 6}" fill="#5f6368">Total tokens per question (both agents), 5 trials. 5-source bar = 1 question.</text>')
    _write("09-crossover", out)
    return ratios


def versus_chart(name, title, subtitle, xlabels, mcp, a2a, ylabel, lower_is_better=True, xtitle="", unit=""):
    """Grouped MCP vs A2A bars per x value, with a winner tag over each pair.
    Built to be read in 10-30 seconds: the title states the takeaway."""
    ymax = 120 if unit == "%" else _nice(max(mcp + a2a) * 1.18)
    out, x, y = _frame(title, ymax, ylabel, xlabels)
    out.append(f'<text x="{W / 2}" y="34" text-anchor="middle" fill="#5f6368">{subtitle}</text>')
    if xtitle:
        out.append(f'<text x="{(L + W - R) / 2}" y="{H - B + 32}" text-anchor="middle" fill="#5f6368">{xtitle}</text>')
    slot = (W - L - R) / len(xlabels)
    bw = slot * 0.32
    for i, (m, a) in enumerate(zip(mcp, a2a)):
        for j, (v, color) in enumerate(((m, MCP_C), (a, A2A_C))):
            bx = x(i) - bw - 1 + j * (bw + 2)
            out.append(f'<rect x="{bx:.1f}" y="{y(v):.1f}" width="{bw:.1f}" height="{y(0) - y(v):.1f}" fill="{color}"/>')
            out.append(f'<text x="{bx + bw / 2:.1f}" y="{y(v) - 3:.1f}" text-anchor="middle" font-size="9" fill="#202124">{_fmt(round(v, 1))}{unit}</text>')
        if abs(m - a) / max(m, a, 1e-9) < 0.05:  # within 5%: call it a tie
            tag, col = "tie", "#5f6368"
        else:
            mcp_wins = (m < a) if lower_is_better else (m > a)
            tag, col = ("MCP wins", MCP_C) if mcp_wins else ("A2A wins", A2A_C)
        out.append(f'<text x="{x(i):.1f}" y="{y(max(m, a)) - 15:.1f}" text-anchor="middle" font-weight="bold" font-size="10" fill="{col}">{tag}</text>')
    _legend(out, [("MCP (5 connections)", MCP_C), ("A2A (1 agent)", A2A_C)])
    _write(name, out)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'v2')
