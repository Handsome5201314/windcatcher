"""Read-only live MCP protocol smoke using the official Python SDK."""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def content_json(result):
    assert not result.isError, result
    assert len(result.content) == 1 and result.content[0].type == "text", result
    return json.loads(result.content[0].text)


async def main():
    root = Path(__file__).resolve().parent.parent
    params = StdioServerParameters(command=sys.executable, args=[str(root / "windcatcher_mcp.py")], cwd=str(root))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {"service_status", "list_cfd_cases", "inspect_cfd_case", "submit_cfd_batch", "get_cfd_batch", "cancel_queued_cfd"}
            status = await session.call_tool("service_status", {})
            status_data = content_json(status)
            assert status_data["worker_alive"] is True
            cases = await session.call_tool("list_cfd_cases", {"limit": 1})
            case_data = content_json(cases)
            rows = case_data["cases"]
            if rows:
                detail = await session.call_tool("inspect_cfd_case", {"case_id": rows[0]["case_id"]})
                detail_data = content_json(detail)
                assert "source_path" not in json.dumps(detail_data)
            invalid = await session.call_tool("get_cfd_batch", {"batch_id": "../../bad"})
            assert invalid.isError
            missing = await session.call_tool("get_cfd_batch", {"batch_id": "batch-0000000000000000"})
            assert missing.isError
            print(json.dumps({"protocol": initialized.protocolVersion, "tools": sorted(names), "service": "ready",
                              "case_count": case_data["total"], "read_only_smoke": "passed"}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
