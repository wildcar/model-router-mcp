"""Manual MCP client probe against a running server (not part of pytest).

Usage: python tests/mcp_probe.py http://127.0.0.1:8011/mcp
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url: str) -> None:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])

            result = await session.call_tool("list_models", {"modality": "chat"})
            n = len(result.content)
            print("list_models content blocks:", n)
            if result.structuredContent is not None:
                data = result.structuredContent
                count = len(data.get("result", data)) if isinstance(data, dict) else data
                print("list_models result:", count)

            usage = await session.call_tool("get_usage", {})
            print("get_usage isError:", usage.isError)
            print("get_usage structured:", usage.structuredContent)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011/mcp"))
