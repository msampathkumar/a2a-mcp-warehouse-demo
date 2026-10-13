"""Run the measured comparison for one level.

    cd test-app
    python -m harness.run --level 1 --trials 1 --paths mcp,a2a

Each (path, trial) gets its own run_id. Before a trial the server-side A2A
workspace is reset (POST /_reset) and the client workspace is cleared, so
trials are independent. After a trial the server events are pulled from
/_stats and saved next to the client events.

Output: test-app/runs/<batch>/  (results.jsonl, client_events.jsonl,
client_spans.jsonl, server_<run_id>.json, batch.json)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .agents import build_a2a_client, build_mcp_client
from .score import is_correct, parse_answer
from .telemetry import EventLog, question_scope, setup_tracing
from .workspace import ClientWorkspace

ROOT = Path(__file__).resolve().parent.parent  # test-app/


def safe_error(e: Exception) -> str:
    """Short, public-safe error text: type + HTTP status, never provider debug payloads."""
    m = re.search(r"\b([45]\d\d) ([A-Z_]{3,})\b", str(e))
    if m:
        return f"{type(e).__name__}: {m.group(1)} {m.group(2)}"
    first = next((ln.strip() for ln in str(e).splitlines() if ln.strip()), "")
    return f"{type(e).__name__}: {first[:160]}"
MAX_CLIENT_LLM_CALLS = int(os.environ.get('MAX_CLIENT_LLM_CALLS', '25'))


def _bool_env(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return (
        default
        if v is None
        else v.strip().lower() in {'1', 'true', 'yes', 'on'}
    )


async def ask(runner: Runner, user_id: str, text: str) -> str:
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id
    )
    final = ''
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role='user', parts=[types.Part(text=text)]),
        run_config=RunConfig(max_llm_calls=MAX_CLIENT_LLM_CALLS),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final = ''.join(p.text or '' for p in event.content.parts)
    return final


async def run_trial(
    path,
    trial,
    level,
    questions,
    answers,
    base_url,
    model,
    out_dir,
    batch_id,
    log,
    ws_cfg,
):
    run_id = f'{batch_id}-{path}-t{trial}'
    async with httpx.AsyncClient(timeout=30) as http:
        catalog = (await http.get(f'{base_url}/catalog.json')).json()
        if catalog['level'] != level:
            raise SystemExit(
                f'server at {base_url} is level {catalog["level"]}, not {level}'
            )
        (
            await http.post(f'{base_url}/_reset', params={'run_id': run_id})
        ).raise_for_status()

    ws = ClientWorkspace(
        *ws_cfg,
        on_cache_hit=lambda tool, via: log.record(
            'cache_hit', tool=tool, via=via
        ),
    )
    if path == 'mcp':
        agent, closers = build_mcp_client(catalog, model, log, ws)
    else:
        agent, closers = build_a2a_client(catalog, model, log)
    runner = Runner(
        app_name=f'client_{path}',
        agent=agent,
        session_service=InMemorySessionService(),
    )

    results = []
    try:
        for q in questions:
            ws.new_question()
            with question_scope(run_id, q['id'], trial, path, level) as span:
                t0, started_at = (
                    time.perf_counter(),
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                )
                error = None
                try:
                    text = await ask(runner, 'harness', q['question'])
                except Exception as e:  # keep going; record the failure
                    text, error = '', safe_error(e)
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                trace_id = format(span.get_span_context().trace_id, '032x')
            answer, parse_err = parse_answer(text)
            exp = answers[q['id']]
            ok = is_correct(answer, exp['expected'], exp['answer_type'])
            row = {
                'batch': batch_id,
                'run_id': run_id,
                'path': path,
                'trial': trial,
                'level': level,
                'q_id': q['id'],
                'sources_needed': q['sources_needed'],
                'started_at': started_at,
                'elapsed_ms': elapsed_ms,
                'trace_id': trace_id,
                'answer': answer,
                'expected': exp['expected'],
                'correct': ok,
                'error': error or parse_err,
                'raw': text[:2000],
            }
            results.append(row)
            print(
                f'  {path} t{trial} {q["id"]}: {"OK " if ok else "BAD"} {elapsed_ms / 1000:6.1f}s  answer={answer!r}'
            )
    finally:
        for c in closers:
            close = getattr(c, 'close', None) or getattr(c, 'aclose', None)
            if close:
                res = close()
                if asyncio.iscoroutine(res):
                    await res

    async with httpx.AsyncClient(timeout=30) as http:
        stats = (
            await http.get(f'{base_url}/_stats', params={'run_id': run_id})
        ).json()
    (out_dir / f'server_{run_id}.json').write_text(json.dumps(stats, indent=1))
    with (out_dir / 'results.jsonl').open('a') as f:
        for r in results:
            f.write(json.dumps(r, default=str) + '\n')
    return results


async def main_async(args) -> Path:
    questions_all = json.loads(
        (ROOT.parent / 'demo-app' / 'tally.json').read_text()
    )['questions']
    answers = json.loads((ROOT / 'answers.json').read_text())['answers']
    questions = [q for q in questions_all if q['level'] == args.level]
    if args.questions:
        wanted = set(args.questions.split(','))
        questions = [q for q in questions if q['id'] in wanted]
    if not questions:
        raise SystemExit('no questions selected')

    tag = f'-{args.tag}' if args.tag else ''
    batch_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + f'-L{args.level}{tag}-' + uuid.uuid4().hex[:4]
    out_dir = ROOT / 'runs' / batch_id
    out_dir.mkdir(parents=True)
    setup_tracing(out_dir / 'client_spans.jsonl')
    log = EventLog(out_dir / 'client_events.jsonl')
    ws_cfg = (
        _bool_env('CACHE_ENABLED', True),
        float(os.environ.get('CACHE_TTL_MIN', '60')),
    )
    base_url = args.base_url or f'http://localhost:{8000 + args.level}'
    async with httpx.AsyncClient(timeout=10) as http:
        server_cfg = (await http.get(f'{base_url}/catalog.json')).json()[
            'config'
        ]
        await http.get(f'{base_url}/healthz')  # warm-up, discarded
    meta = {
        'batch': batch_id,
        'level': args.level,
        'trials': args.trials,
        'paths': args.paths,
        'questions': [q['id'] for q in questions],
        'client_model': args.model,
        'client_cache': {'enabled': ws_cfg[0], 'ttl_min': ws_cfg[1]},
        'max_client_llm_calls': MAX_CLIENT_LLM_CALLS,
        'server_config': server_cfg,
        'base_url': base_url,
        'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (out_dir / 'batch.json').write_text(json.dumps(meta, indent=2))
    print(
        f'batch {batch_id}: level {args.level}, {len(questions)} questions, paths={args.paths}, trials={args.trials}'
    )
    for trial in range(args.trial_offset + 1, args.trial_offset + args.trials + 1):
        for path in args.paths.split(','):
            await run_trial(
                path,
                trial,
                args.level,
                questions,
                answers,
                base_url,
                args.model,
                out_dir,
                batch_id,
                log,
                ws_cfg,
            )
    meta['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    (out_dir / 'batch.json').write_text(json.dumps(meta, indent=2))
    return out_dir


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--level', type=int, required=True)
    p.add_argument('--trials', type=int, default=1)
    p.add_argument('--paths', default='mcp,a2a')
    p.add_argument(
        '--questions',
        default='',
        help='comma-separated ids, e.g. L1-Q01,L1-Q02',
    )
    p.add_argument('--base-url', default='')
    p.add_argument('--tag', default='', help='label shared by batches of one sweep')
    p.add_argument('--trial-offset', type=int, default=0, help='number trials from offset+1')
    p.add_argument(
        '--model', default=os.environ.get('MODEL', 'gemini-3.5-flash-lite')
    )
    args = p.parse_args()
    out = asyncio.run(main_async(args))
    from .report import write_report

    print(f'report: {write_report(out)}')


if __name__ == '__main__':
    main()
