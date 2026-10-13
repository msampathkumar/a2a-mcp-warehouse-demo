"""The Warehouse Analyst: one ADK agent, served over A2A.

It reaches every data source of the current level through the same
DataManager the MCP servers use (independent path, same rules), combines
results in its own workspace, and returns only the answer.
"""

from __future__ import annotations

import anyio
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from .config import Settings
from .telemetry import Recorder
from .workspace import Workspace
from .playbook import build_playbook

INSTRUCTION = """You are the Warehouse Analyst. You answer data questions about a sandwich-shop
data warehouse. Each data source holds ONE table and lives in a different place.

Sources available now: {tables}

Rules:
1. A source query is ONE read-only SELECT over that source's table. No JOIN, CTE or UNION.
   Push filters and aggregations (WHERE, GROUP BY, SUM, COUNT) into the source query.
2. To combine sources, pull (already aggregated) results into your workspace with
   save_result, then join them with local_sql. local_sql can JOIN saved frames.
3. Never fetch data you already hold. Call list_workspace first; reuse saved frames.
{notes_rule}
4. Money columns are integer cents. Keep cents unless the question says otherwise.
5. Only answer questions about this warehouse. Refuse anything else.

Finish with ONLY a JSON object, no markdown fences:
{{"answer": <number | string | list>, "explanation": "<one short sentence>"}}
"""

NOTES_RULE = (
    '   Call read_notes at the start. After you learn a schema fact or join key, '
    'save it with write_notes (keep notes short; rewrite the whole file).'
)


def build_agent(
    settings: Settings, ws: Workspace, recorder: Recorder
) -> LlmAgent:
    async def _t(fn, *args):
        return await anyio.to_thread.run_sync(lambda: fn(*args))

    async def list_sources() -> dict:
        """List the data sources (one table each) you can query."""
        return {'sources': settings.tables}

    async def describe_source(source: str) -> dict:
        """Schema, total row count and 3 sample rows for one source."""
        return await _t(ws.describe, source)

    async def query_source(source: str, sql: str, page: int = 1) -> dict:
        """Run ONE read-only SELECT over a single source's table. Returns one page
        (cols, rows, n, total, page, more). Use ORDER BY for stable paging."""
        return await _t(ws.query, source, sql, page)

    async def save_result(name: str, source: str, sql: str) -> dict:
        """Fetch ALL pages of a single-source SELECT into your workspace as frame
        `name` (a simple identifier). Aggregate in SQL first to keep it small."""
        return await _t(ws.save_result, name, source, sql)

    async def list_workspace() -> dict:
        """List frames already saved in your workspace (reuse them)."""
        return await _t(ws.list_workspace)

    async def semi_join_fetch(
        name: str, source: str, columns: str, key_column: str,
        frame: str, frame_column: str, extra_where: str = "",
    ) -> dict:
        """Fetch rows of `source` (selecting `columns`) whose `key_column` is IN the
        values of `frame.frame_column`, in batches of 200 keys. Saves frame `name`.
        Use to get order_items for a filtered set of orders."""
        return await _t(ws.semi_join_fetch, name, source, columns, key_column, frame, frame_column, extra_where)

    async def local_sql(sql: str) -> dict:
        """Run DuckDB SQL over your saved frames (JOINs allowed). Max 200 rows back."""
        return await _t(ws.local_sql, sql)

    async def read_notes() -> dict:
        """Read your saved notes about schemas and join keys."""
        return await _t(ws.read_notes)

    async def write_notes(text: str) -> dict:
        """Replace your notes file with `text` (max 4000 chars)."""
        return await _t(ws.write_notes, text)

    llm_calls: dict[str, int] = {}

    def cap_llm_calls(callback_context: CallbackContext, llm_request):
        """Cost guard: stop one invocation after MAX_AGENT_LLM_CALLS model calls."""
        inv = callback_context.invocation_id
        llm_calls[inv] = llm_calls.get(inv, 0) + 1
        if llm_calls[inv] > settings.max_agent_llm_calls:
            recorder.record('llm_cap_hit', path='a2a', limit=settings.max_agent_llm_calls)
            return LlmResponse(
                content=types.Content(
                    role='model',
                    parts=[types.Part(text='{"answer": null, "explanation": "stopped: LLM call limit reached"}')],
                )
            )
        return None

    def record_llm_usage(
        callback_context: CallbackContext, llm_response: LlmResponse
    ):
        u = llm_response.usage_metadata
        if u is None or llm_response.partial:
            return None
        recorder.record(
            'llm_call',
            path='a2a',
            agent='warehouse_analyst',
            tokens_in=u.prompt_token_count or 0,
            tokens_out=(u.candidates_token_count or 0)
            + (u.thoughts_token_count or 0),
        )
        return None

    playbook = build_playbook(settings, ws.dm) if settings.playbook_enabled else ''
    instruction_text = playbook + '\n\n' + INSTRUCTION.format(
        tables=', '.join(settings.tables),
        notes_rule=NOTES_RULE if settings.notes_enabled else '',
    )

    tools = [
        list_sources,
        describe_source,
        query_source,
        save_result,
        list_workspace,
        local_sql,
        semi_join_fetch,
    ]
    if settings.notes_enabled:
        tools += [read_notes, write_notes]

    return LlmAgent(
        name='warehouse_analyst',
        model=settings.model,
        description=(
            'Answers data questions over the sandwich-shop warehouse. Reaches every data '
            'source itself, joins and aggregates, and returns only the final answer.'
        ),
        instruction=lambda _ctx: instruction_text,  # callable: skip ADK {state} templating
        tools=tools,
        generate_content_config=types.GenerateContentConfig(temperature=0),
        before_model_callback=cap_llm_calls,
        after_model_callback=record_llm_usage,
    )
