"""The two measured client agents. Same model, same base prompt, same
temperature; only the tools differ.

  Path M ("mcp"): N McpToolsets (one per source) + local save_result /
                  list_workspace / local_sql. The client joins the data.
  Path A ("a2a"): one tool, the remote warehouse_analyst agent over A2A.
                  The remote agent joins the data and returns the answer.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from opentelemetry import baggage

from .telemetry import EventLog, httpx_inject_hook
from .workspace import ClientWorkspace

BASE_PROMPT = """You are a data assistant for a sandwich-shop data warehouse.
Answer the user's question using only your tools. Money columns are integer cents.
Finish with ONLY a JSON object, no markdown fences:
{{"answer": <number | string | list>, "explanation": "<one short sentence>"}}

{path_rules}"""

MCP_RULES = """Data access: one MCP server per table ({tables}); each has describe_<table> and query_<table>.
A query is ONE SELECT over that one table: no JOIN, CTE or UNION. Push filters and
aggregation (WHERE, GROUP BY, SUM, COUNT) into the SQL.
To combine tables: save_result each needed (aggregated) query into your workspace,
then JOIN the frames with local_sql. Call list_workspace first and reuse frames;
never fetch data you already hold."""

A2A_RULES = """Data access: the warehouse_analyst agent answers data questions over every
table in the warehouse. Send it the user's question, then return its answer in the
required JSON."""

MCP_TOOL_PREFIXES = ('describe_', 'query_')


def _baggage_headers(_ctx: Any = None) -> dict[str, str]:
    """Stable per question, so ADK reuses one MCP session per question."""
    items = [
        f'{k}={baggage.get_baggage(k)}'
        for k in ('run_id', 'q_id', 'trial', 'path')
        if baggage.get_baggage(k)
    ]
    return {'baggage': ','.join(items)} if items else {}


def _tool_kind(name: str) -> str:
    if name.startswith(MCP_TOOL_PREFIXES):
        return 'mcp'
    if name == 'warehouse_analyst':
        return 'a2a'
    return 'local'


class Instrumentation:
    """ADK callbacks that time every tool call and count LLM usage."""

    def __init__(self, log: EventLog, ws: ClientWorkspace | None) -> None:
        self.log, self.ws = log, ws
        self._started: dict[str, float] = {}
        self._served_from_cache: set[str] = set()

    def before_tool(
        self, tool: BaseTool, args: dict, tool_context: ToolContext
    ):
        self._started[tool_context.function_call_id] = time.perf_counter()
        if self.ws and _tool_kind(tool.name) == 'mcp':
            hit = self.ws.cached(tool.name, args)
            if hit is not None:
                self._served_from_cache.add(tool_context.function_call_id)
                return hit  # skip the MCP call
        return None

    def after_tool(self, tool: BaseTool, args: dict, tool_context: ToolContext, tool_response: dict):
        started = self._started.pop(tool_context.function_call_id, None)
        cached = tool_context.function_call_id in self._served_from_cache
        self._served_from_cache.discard(tool_context.function_call_id)
        ms = round((time.perf_counter() - started) * 1000, 2) if started else None
        kind = _tool_kind(tool.name)
        # cached=True: answered by the client cache, no network call was made.
        self.log.record("tool_call", tool=tool.name, tool_kind=kind, ms=ms, cached=cached, args=_short(args))
        if self.ws and kind == "mcp" and not cached:
            self.ws.store(tool.name, args, tool_response)
        return None

    def after_model(
        self, callback_context: CallbackContext, llm_response: LlmResponse
    ):
        u = llm_response.usage_metadata
        if u is not None and not llm_response.partial:
            self.log.record(
                'llm_call',
                agent='client',
                tokens_in=u.prompt_token_count or 0,
                tokens_out=(u.candidates_token_count or 0)
                + (u.thoughts_token_count or 0),
            )
        return None


def _short(args: dict) -> dict:
    return {k: (v[:300] if isinstance(v, str) else v) for k, v in args.items()}


def _parse_mcp_text(resp: dict) -> dict:
    """McpTool returns a CallToolResult dict; our servers put compact JSON in text."""
    try:
        for c in resp.get('content', []):
            if c.get('type') == 'text':
                return json.loads(c['text'])
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return {'error': f'unexpected MCP response: {str(resp)[:200]}'}


def build_mcp_client(
    catalog: dict, model: str, log: EventLog, ws: ClientWorkspace
) -> tuple[LlmAgent, list]:
    inst = Instrumentation(log, ws)
    toolsets = {
        m['url'].rstrip('/').rsplit('/', 1)[-1]: McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=m['url'], timeout=30
            ),
            header_provider=_baggage_headers,
        )
        for m in catalog['mcp_servers']
    }
    query_tools: dict[str, Any] = {}

    async def save_result(
        name: str, source: str, sql: str, tool_context: ToolContext
    ) -> dict:
        """Fetch ALL pages of ONE single-table SELECT from `source` into your
        workspace as frame `name` (simple identifier). Aggregate in SQL first."""
        if source not in toolsets:
            return {
                'error': f"unknown source '{source}'. Available: {list(toolsets)}"
            }
        if source not in query_tools:
            for t in await toolsets[source].get_tools():
                if t.name == f'query_{source}':
                    query_tools[source] = t
        tool = query_tools[source]

        async def fetch(src: str, q: str, page: int) -> dict:
            args = {'sql': q, 'page': page}
            hit = ws.cached(tool.name, args)
            if hit is not None:
                return _parse_mcp_text(hit)
            t0 = time.perf_counter()
            resp = await tool.run_async(args=args, tool_context=tool_context)
            log.record(
                'tool_call',
                tool=tool.name,
                tool_kind='mcp',
                via='save_result',
                ms=round((time.perf_counter() - t0) * 1000, 2),
                args=_short(args),
            )
            ws.store(tool.name, args, resp)
            return _parse_mcp_text(resp)

        return await ws.save_result(name, source, sql, fetch)

    async def list_workspace() -> dict:
        """List frames already saved in your workspace (reuse them)."""
        return ws.list_workspace()

    async def local_sql(sql: str) -> dict:
        """Run DuckDB SQL over your saved frames (JOINs allowed). Max 200 rows back."""
        return ws.local_sql(sql)

    agent = LlmAgent(
        name='client_mcp',
        model=model,
        instruction=lambda _c: BASE_PROMPT.format(
            path_rules=MCP_RULES.format(tables=', '.join(catalog['tables']))
        ),
        tools=[*toolsets.values(), save_result, list_workspace, local_sql],
        generate_content_config=types.GenerateContentConfig(temperature=0),
        before_tool_callback=inst.before_tool,
        after_tool_callback=inst.after_tool,
        after_model_callback=inst.after_model,
    )
    return agent, list(toolsets.values())


def build_a2a_client(
    catalog: dict, model: str, log: EventLog
) -> tuple[LlmAgent, list]:
    inst = Instrumentation(log, None)
    card_url = catalog['a2a_agents'][0]['agent_card']
    http = httpx.AsyncClient(
        timeout=600, event_hooks={'request': [httpx_inject_hook]}
    )
    remote = RemoteA2aAgent(
        name='warehouse_analyst',
        agent_card=card_url,
        description='Answers data questions over every table in the warehouse and returns the answer.',
        httpx_client=http,
        timeout=600,
    )
    agent = LlmAgent(
        name='client_a2a',
        model=model,
        instruction=lambda _c: BASE_PROMPT.format(path_rules=A2A_RULES),
        tools=[AgentTool(agent=remote)],
        generate_content_config=types.GenerateContentConfig(temperature=0),
        before_tool_callback=inst.before_tool,
        after_tool_callback=inst.after_tool,
        after_model_callback=inst.after_model,
    )
    return agent, [http]
