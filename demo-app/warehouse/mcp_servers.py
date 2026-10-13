"""One MCP server per data source.

Each server exposes exactly two read-only tools for its own table:
  describe_<source>()            -> schema, row count, sample rows
  query_<source>(sql, page=1)    -> one page of a single-table SELECT

At level N there are N servers, mounted at /mcp/<source>/.
"""

from __future__ import annotations

import json

import anyio
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .data_manager import DataManager

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _compact(obj: dict) -> str:
    return json.dumps(obj, separators=(',', ':'), default=str)


def build_mcp_server(source: str, dm: DataManager) -> MCPServer:
    server = MCPServer(
        name=f'warehouse-{source}',
        instructions=(
            f"Read-only access to the '{source}' table only. "
            f'SQL must be a single SELECT over {source}; no JOIN/CTE/UNION. '
            f"Results are paged ({dm.row_cap} rows/page): pass page=2,3,... while 'more' is true. "
            'Push filtering and aggregation into SQL to keep results small.'
        ),
    )

    async def describe() -> str:
        return _compact(
            await anyio.to_thread.run_sync(
                lambda: dm.describe(source, path='mcp')
            )
        )

    async def query(sql: str, page: int = 1) -> str:
        return _compact(
            await anyio.to_thread.run_sync(
                lambda: dm.query(source, sql, page, path='mcp')
            )
        )

    server.add_tool(
        describe,
        name=f'describe_{source}',
        description=f"Schema, total row count and 3 sample rows of the '{source}' table.",
        annotations=READ_ONLY,
        structured_output=False,
    )
    server.add_tool(
        query,
        name=f'query_{source}',
        description=(
            f"Run ONE read-only SELECT over the '{source}' table (DuckDB SQL). "
            'No JOIN/CTE/UNION/other tables. Returns compact JSON: '
            '{t, cols, rows, n, total, page, more, ms}. Use ORDER BY for stable paging.'
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    return server
