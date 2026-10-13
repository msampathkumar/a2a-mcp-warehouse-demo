# Questions List — manual testing

Use this to try the demo with your own coding agent (Antigravity, Gemini CLI, Claude Code, ...).

Answers are not listed here on purpose: the agent under test could read this file. Run `python test-app/build_tally.py` to see them, or check `test-app/answers.json` after your run.

## Setup

1. `make serve LEVEL=1` (level N listens on port `800N`).
2. Open `http://localhost:8001/catalog.json`.
3. Pick **one** path per session:
   - **MCP**: add each `mcp_servers[].url` as a streamable-HTTP MCP server in your tool.
   - **A2A**: give your tool the `a2a_agents[].agent_card` URL (e.g. with the `a2a` CLI).
4. Paste the questions for that level from `http://localhost:8001/` one at a time.

## What to validate

| Check | How | Expected |
| :--- | :--- | :--- |
| Answer is right | Compare with `test-app/answers.json` | Exact for counts and cent totals; ±0.5% for averages |
| One table per query | `curl localhost:8001/_stats` → `events[].sql` | No JOINs; any JOIN attempt shows `ok:false` |
| Who did the joining | Same events, `path` field | MCP path: many `dm_call` from your agent. A2A path: `dm_call` with `path:"a2a"` and one message from you |
| Calls you made | Your tool's log | MCP: one tool call per source query. A2A: one message per question |
| Trace | `demo-app/runs/level<N>_spans.jsonl` (or Jaeger) | Server spans for each request; DataManager spans nested inside |

Manual sessions have no `run_id` baggage, so their events land under `run_id=manual` and are excluded from the harness tables.

## Expected outcome by level

| Level | Sources per question | Expected pattern |
| :---: | :---: | :--- |
| 1 | 1 | MCP is as accurate and faster: one query answers it. A2A adds a second agent's round-trips. |
| 2 | 1–2 | MCP starts needing 2 sources and local joins; A2A still one message. |
| 3 | 1–3 | MCP calls and client tokens grow with sources; A2A stays at one message per question. |
| 4 | 1–4 | Filtered cross-source questions (one day of orders → items). MCP client carries the join. |
| 5 | 1–5 | Widest joins. Where the protocols diverge most. |

These are hypotheses. The harness reports what actually happens, including when they are wrong.

## Questions

The questions are the `questions` in [`demo-app/tally.json`](../demo-app/tally.json), generated from [`test-app/questions.yaml`](../test-app/questions.yaml). The homepage shows the current level's list.
