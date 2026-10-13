"""Build the comparison table from a batch directory. Every number is derived
from raw events: server_<run_id>.json (DataManager calls, A2A agent LLM calls,
cache hits) and client_events.jsonl (client tool calls, client LLM calls).

    python -m harness.report runs/<batch>
"""

from __future__ import annotations

import json
import shutil
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

from .score import is_correct, parse_answer

ROOT = Path(__file__).resolve().parent.parent


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def _fmt(v, nd=1):
    if v is None:
        return '–'
    if isinstance(v, float):
        return f'{v:,.{nd}f}'
    return f'{v:,}'


def _rescore(r: dict, answers: dict) -> dict:
    """Re-derive answer/correct from the recorded raw response with the current
    scorer, so a scorer fix is applied to old batches without re-running agents."""
    exp = answers[r['q_id']]
    if not r.get('raw'):  # run failed before any response; keep its error
        return {**r, 'answer': None, 'correct': False}
    answer, err = parse_answer(r['raw'])
    return {
        **r,
        'recorded_correct': r.get('correct'),
        'answer': answer,
        'correct': is_correct(answer, exp['expected'], exp['answer_type']),
        'error': err,
    }


def load(batch_dir: Path):
    meta = json.loads((batch_dir / 'batch.json').read_text())
    answers = json.loads((ROOT / 'answers.json').read_text())['answers']
    results = [
        _rescore(json.loads(l), answers)
        for l in (batch_dir / 'results.jsonl').read_text().splitlines()
        if l.strip()
    ]
    client = [
        json.loads(l)
        for l in (batch_dir / 'client_events.jsonl').read_text().splitlines()
        if l.strip()
    ]
    server = []
    for f in sorted(batch_dir.glob('server_*.json')):
        server.extend(json.loads(f.read_text())['events'])
    return meta, results, client, server


def per_attempt(results, client, server):
    """One metrics dict per (run_id, q_id) attempt."""
    rows = []
    for r in results:
        key = (r['run_id'], r['q_id'])
        ce = [e for e in client if (e['run_id'], e['q_id']) == key]
        se = [e for e in server if (e['run_id'], e['q_id']) == key]
        tools = [e for e in ce if e['kind'] == 'tool_call']
        mcp_calls = [
            e for e in tools if e['tool_kind'] == 'mcp' and not e.get('cached')
        ]
        a2a_calls = [e for e in tools if e['tool_kind'] == 'a2a']
        dm = [e for e in se if e['kind'] == 'dm_call']
        client_llm = [e for e in ce if e['kind'] == 'llm_call']
        remote_llm = [e for e in se if e['kind'] == 'llm_call']
        rows.append(
            {
                **r,
                'dm_calls_mcp': sum(1 for e in dm if e['path'] == 'mcp'),
                'dm_calls_a2a': sum(1 for e in dm if e['path'] == 'a2a'),
                'dm_rejected': sum(1 for e in dm if not e['ok']),
                'mcp_calls': len(mcp_calls),
                'mcp_call_ms': _mean([e['ms'] for e in mcp_calls]),
                'a2a_calls': len(a2a_calls),
                'a2a_call_ms': _mean([e['ms'] for e in a2a_calls]),
                'client_cache_hits': sum(
                    1 for e in ce if e['kind'] == 'cache_hit'
                ),
                'server_cache_hits': sum(
                    1 for e in se if e['kind'] == 'cache_hit'
                ),
                'client_llm_calls': len(client_llm),
                'remote_llm_calls': len(remote_llm),
                'client_tokens': sum(
                    e['tokens_in'] + e['tokens_out'] for e in client_llm
                ),
                'remote_tokens': sum(
                    e['tokens_in'] + e['tokens_out'] for e in remote_llm
                ),
                'trace_ids_server': sorted(
                    {e['trace_id'] for e in se if e.get('trace_id')}
                ),
            }
        )
    return rows


def write_report(batch_dir: Path) -> Path:
    meta, results, client, server = load(batch_dir)
    rows = per_attempt(results, client, server)
    tally = {
        q['id']: q
        for q in json.loads(
            (ROOT.parent / 'demo-app' / 'tally.json').read_text()
        )['questions']
    }
    by_q = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_q[r['q_id']][r['path']].append(r)

    def m(q, path, field):
        return _mean([r[field] for r in by_q[q][path]])

    def pct(q, path):
        rs = by_q[q][path]
        return (
            f'{100 * sum(r["correct"] for r in rs) / len(rs):.0f}%'
            if rs
            else '–'
        )

    n_tables = len(meta['server_config']['tables'])
    lines = [
        f'# Results — level {meta["level"]} — batch `{meta["batch"]}`',
        '',
        f'- Trials: {meta["trials"]} · paths: {meta["paths"]} · questions: {len(meta["questions"])}',
        f'- Model (client + remote): `{meta["client_model"]}` / `{meta["server_config"]["model"]}`, temperature 0',
        f'- Server: {n_tables} table(s), row cap {meta["server_config"]["row_cap"]}, '
        f'source latency {meta["server_config"]["source_latency_ms"]} ms, cache '
        f'{"on" if meta["server_config"]["cache_enabled"] else "off"} '
        f'(TTL {meta["server_config"]["cache_ttl_min"]} min)',
        f'- Client cache: {"on" if meta["client_cache"]["enabled"] else "off"} (TTL {meta["client_cache"]["ttl_min"]} min)',
        f'- A2A agent notes tools (read_notes/write_notes): '
        f'{"on" if meta["server_config"].get("notes_enabled") else "off"}. '
        'The MCP client has no equivalent; this is the A2A agent\'s "learning" feature by design.',
        f'- Started {meta["started_at"]} · finished {meta.get("finished_at", "–")}',
        *(
            ['- **Single trial** at temperature 0: no spread measured; treat as indicative.']
            if meta['trials'] == 1
            else []
        ),
        '- Raw evidence: [`' + meta['batch'] + '/`](' + meta['batch'] + '/) '
        '(results.jsonl, client_events.jsonl, server_*.json, batch.json).',
        '',
        'Counts are means over trials. DataManager counts come from server events; '
        'tool calls and durations are measured by the client.',
        '',
        '## Comparison',
        '',
        '| # | Tables | Sources needed | Question | MCP→DM calls | A2A calls | A2A→DM calls '
        '| MCP call avg ms | A2A call avg ms | User API calls (MCP) | User API calls (A2A) '
        '| End-to-end s (MCP) | End-to-end s (A2A) | Correct (MCP) | Correct (A2A) |',
        '|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for q in meta['questions']:
        t = tally[q]
        e2e_m, e2e_a = m(q, 'mcp', 'elapsed_ms'), m(q, 'a2a', 'elapsed_ms')
        lines.append(
            '| '
            + ' | '.join(
                [
                    q,
                    str(n_tables),
                    str(len(t['sources_needed'])),
                    t['question'].replace('|', '\\|'),
                    _fmt(m(q, 'mcp', 'dm_calls_mcp')),
                    _fmt(m(q, 'a2a', 'a2a_calls')),
                    _fmt(m(q, 'a2a', 'dm_calls_a2a')),
                    _fmt(m(q, 'mcp', 'mcp_call_ms'), 0),
                    _fmt(m(q, 'a2a', 'a2a_call_ms'), 0),
                    _fmt(m(q, 'mcp', 'mcp_calls')),
                    _fmt(m(q, 'a2a', 'a2a_calls')),
                    _fmt(e2e_m / 1000 if e2e_m else None),
                    _fmt(e2e_a / 1000 if e2e_a else None),
                    pct(q, 'mcp'),
                    pct(q, 'a2a'),
                ]
            )
            + ' |'
        )

    def tot(path, field):
        return sum(r[field] for r in rows if r['path'] == path)

    def acc(path):
        rs = [r for r in rows if r['path'] == path]
        return f'{sum(r["correct"] for r in rs)}/{len(rs)}' if rs else '–'

    lines += [
        '',
        '## Totals (all attempts)',
        '',
        '| Metric | MCP path | A2A path |',
        '|---|---:|---:|',
        f'| Correct | {acc("mcp")} | {acc("a2a")} |',
        f'| User → server calls (MCP tool calls / A2A messages) | {tot("mcp", "mcp_calls")} | {tot("a2a", "a2a_calls")} |',
        f'| DataManager calls | {tot("mcp", "dm_calls_mcp")} | {tot("a2a", "dm_calls_a2a")} |',
        f'| DataManager calls rejected (rule broken) | {tot("mcp", "dm_rejected")} | {tot("a2a", "dm_rejected")} |',
        f'| Cache hits (client / server) | {tot("mcp", "client_cache_hits")} / – | – / {tot("a2a", "server_cache_hits")} |',
        f"| LLM calls: user's agent | {tot('mcp', 'client_llm_calls')} | {tot('a2a', 'client_llm_calls')} |",
        f'| LLM calls: remote A2A agent | – | {tot("a2a", "remote_llm_calls")} |',
        f'| End-to-end time, s | {tot("mcp", "elapsed_ms") / 1000:,.1f} | {tot("a2a", "elapsed_ms") / 1000:,.1f} |',
        '',
        '## Appendix: tokens',
        '',
        '| Metric | MCP path | A2A path |',
        '|---|---:|---:|',
        f"| User's agent tokens (in+out) | {tot('mcp', 'client_tokens'):,} | {tot('a2a', 'client_tokens'):,} |",
        f'| Remote A2A agent tokens (in+out) | – | {tot("a2a", "remote_tokens"):,} |',
        f'| Total tokens | {tot("mcp", "client_tokens"):,} | {tot("a2a", "client_tokens") + tot("a2a", "remote_tokens"):,} |',
        '',
        '## Appendix: per attempt',
        '',
        '| Path | Trial | # | Correct | Answer | Expected | s | Trace |',
        '|---|---:|---|---|---|---|---:|---|',
    ]
    for r in rows:
        lines.append(
            f'| {r["path"]} | {r["trial"]} | {r["q_id"]} | {"yes" if r["correct"] else "no"} | '
            f'`{json.dumps(r["answer"])[:60]}` | `{json.dumps(r["expected"])[:60]}` | '
            f'{r["elapsed_ms"] / 1000:.1f} | `{r["trace_id"][:12]}…` |'
        )
    rescored = [r for r in rows if r.get('recorded_correct') is not None and r['recorded_correct'] != r['correct']]
    lines += ['', '## Re-scored attempts', '']
    if rescored:
        lines.append(
            'The scorer was fixed after the run; these attempts were re-scored from their recorded raw reply:'
        )
        lines.append('')
        for r in rescored:
            lines.append(
                f'- {r["path"]} t{r["trial"]} {r["q_id"]}: recorded '
                f'{"correct" if r["recorded_correct"] else "wrong"} → now '
                f'{"correct" if r["correct"] else "wrong"}. Raw reply: `{r["raw"][:160].replace(chr(10), " ⏎ ").replace("`", "'")}`'
            )
    else:
        lines.append('None.')
    out = ROOT.parent / 'docs' / 'results' / f'{meta["batch"]}.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(lines) + '\n')
    evidence = out.parent / meta['batch']
    evidence.mkdir(exist_ok=True)
    for f in ['batch.json', 'results.jsonl', 'client_events.jsonl', *[p.name for p in batch_dir.glob('server_*.json')]]:
        shutil.copyfile(batch_dir / f, evidence / f)
    (batch_dir / 'attempts.json').write_text(
        json.dumps(rows, indent=1, default=str)
    )
    return out


if __name__ == '__main__':
    print(write_report(Path(sys.argv[1])))
