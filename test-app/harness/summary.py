"""Cross-level summary: aggregates published batches into docs/results/summary.md.

    python -m harness.summary            # uses the newest batch per level in docs/results/

Every number is re-derived from the raw evidence copied next to each batch
report (docs/results/<batch>/), via report.load + report.per_attempt.
"""

from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from pathlib import Path

from .report import load, per_attempt

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT.parent / 'docs' / 'results'


def latest_batches(tag: str = '') -> dict[int, list[Path]]:
    """With a tag: every batch of that sweep. Without: the newest batch per level."""
    by_level: dict[int, list[Path]] = {}
    for d in sorted(p for p in RESULTS.iterdir() if p.is_dir() and (p / 'batch.json').exists()):
        level = json.loads((d / 'batch.json').read_text())['level']
        if tag:
            if f'-{tag}-' in d.name:
                by_level.setdefault(level, []).append(d)
        else:
            by_level[level] = [d]
    return dict(sorted(by_level.items()))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else 0.0


def _agg(rows: list[dict]) -> dict:
    m = [r for r in rows if r['path'] == 'mcp']
    a = [r for r in rows if r['path'] == 'a2a']
    return {
        'n_q': len({r['q_id'] for r in rows}),
        'acc_m': sum(r['correct'] for r in m) / len(m) if m else 0,
        'acc_a': sum(r['correct'] for r in a) / len(a) if a else 0,
        'calls_m': _mean([r['mcp_calls'] for r in m]),
        'calls_a': _mean([r['a2a_calls'] for r in a]),
        'dm_m': _mean([r['dm_calls_mcp'] for r in m]),
        'dm_a': _mean([r['dm_calls_a2a'] for r in a]),
        'e2e_m': _mean([r['elapsed_ms'] for r in m]) / 1000,
        'e2e_a': _mean([r['elapsed_ms'] for r in a]) / 1000,
        'llm_m': _mean([r['client_llm_calls'] for r in m]),
        'llm_a_client': _mean([r['client_llm_calls'] for r in a]),
        'llm_a_remote': _mean([r['remote_llm_calls'] for r in a]),
        'tok_m': _mean([r['client_tokens'] for r in m]),
        'tok_a_client': _mean([r['client_tokens'] for r in a]),
        'tok_a_total': _mean(
            [r['client_tokens'] + r['remote_tokens'] for r in a]
        ),
    }


def _row(label, g) -> str:
    return (
        f'| {label} | {g["n_q"]} | {g["acc_m"]:.0%} | {g["acc_a"]:.0%} | {g["calls_m"]:.1f} | {g["calls_a"]:.1f} '
        f'| {g["dm_m"]:.1f} | {g["dm_a"]:.1f} | {g["e2e_m"]:.1f} | {g["e2e_a"]:.1f} '
        f'| {g["tok_m"]:,.0f} | {g["tok_a_client"]:,.0f} | {g["tok_a_total"]:,.0f} |'
    )


HEADER = (
    "| {k} | Questions | Correct MCP | Correct A2A | Calls from user's agent (MCP) | Calls from user's agent (A2A) "
    '| DataManager calls (MCP) | DataManager calls (A2A) | End-to-end s (MCP) | End-to-end s (A2A) '
    "| Tokens: user's agent (MCP) | Tokens: user's agent (A2A) | Tokens: total (A2A) |\n"
    '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|'
)


def _xychart(
    title: str, xs: list[str], m: list[float], a: list[float], y_label: str
) -> list[str]:
    top = max([*m, *a, 1]) * 1.15
    return [
        '```mermaid',
        '%%{init: {"themeVariables": {"xyChart": {"plotColorPalette": "#d93025, #1a73e8"}}}}%%',  # red = MCP, blue = A2A
        'xychart-beta',
        f'    title "{title}"',
        '    x-axis [' + ', '.join(f'"{x}"' for x in xs) + ']',
        f'    y-axis "{y_label}" 0 --> {top:.1f}',
        '    line [' + ', '.join(f'{v:.2f}' for v in m) + ']',
        '    line [' + ', '.join(f'{v:.2f}' for v in a) + ']',
        '```',
        '',
        'Red line: MCP path · Blue line: A2A path.',
        '',
    ]


def main(tag: str = '') -> Path:
    batches = latest_batches(tag)
    all_rows, trials = [], defaultdict(int)
    for level, dirs in batches.items():
        for d in dirs:
            meta, results, client, server = load(d)
            trials[level] += meta['trials']
            all_rows += per_attempt(results, client, server)

    by_level = defaultdict(list)
    by_sources = defaultdict(list)
    for r in all_rows:
        by_level[r['level']].append(r)
        by_sources[len(r['sources_needed'])].append(r)

    src_keys = sorted(by_sources)
    src_aggs = [_agg(by_sources[k]) for k in src_keys]
    lines = [
        '# Summary — all levels',
        '',
        f"Sweep: `{tag or 'newest batch per level'}`. Batches used:",
        '',
        *[
            f'- Level {lvl} ({trials[lvl]} trial(s)): ' + ', '.join(f'[`{d.name}`]({d.name}.md)' for d in dirs)
            for lvl, dirs in batches.items()
        ],
        '',
        "Values are means per question attempt. Calls from the user's agent = MCP tool calls that "
        'reached a server (cache hits excluded) or A2A messages sent.',
        '',
        '## By number of sources a question needs',
        '',
        HEADER.format(k='Sources'),
        *[_row(str(k), g) for k, g in zip(src_keys, src_aggs)],
        '',
        '## By level',
        '',
        HEADER.format(k='Level'),
        *[_row(str(lvl), _agg(rows)) for lvl, rows in sorted(by_level.items())],
        '',
        '## Charts (by sources needed)',
        '',
        *_xychart(
            "Calls from the user's agent per question",
            [str(k) for k in src_keys],
            [g['calls_m'] for g in src_aggs],
            [g['calls_a'] for g in src_aggs],
            'calls',
        ),
        *_xychart(
            'End-to-end seconds per question',
            [str(k) for k in src_keys],
            [g['e2e_m'] for g in src_aggs],
            [g['e2e_a'] for g in src_aggs],
            'seconds',
        ),
        *_xychart(
            "Tokens in the user's agent per question",
            [str(k) for k in src_keys],
            [g['tok_m'] for g in src_aggs],
            [g['tok_a_client'] for g in src_aggs],
            'tokens',
        ),
    ]
    out = RESULTS / 'summary.md'
    out.write_text('\n'.join(lines) + '\n')
    (RESULTS / 'summary.json').write_text(
        json.dumps(
            {
                'batches': {lvl: [d.name for d in dirs] for lvl, dirs in batches.items()},
                'by_sources': {k: g for k, g in zip(src_keys, src_aggs)},
                'by_level': {
                    lvl: _agg(rows) for lvl, rows in sorted(by_level.items())
                },
            },
            indent=1,
        )
    )
    return out


if __name__ == '__main__':
    import sys

    print(main(sys.argv[1] if len(sys.argv) > 1 else ''))
