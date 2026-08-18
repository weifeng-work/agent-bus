"""用 MCP 客户端协议连接打包产物的 --mcp stdio，验证工具列表。"""
import asyncio
import sys
sys.path.insert(0, r"C:\Users\IKUN\Project\智能体协作方案\agent-bus")

import anyio
import mcp.types as types
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


async def main(exe_path: str):
    params = StdioServerParameters(
        command=exe_path,
        args=["--mcp", "--role=worker", "--agent-id=ctl-mcp-test", "--broker-host=127.0.0.1"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("MCP 会话已初始化")
            tools = await session.list_tools()
            print(f"工具数: {len(tools.tools)}")
            names = [t.name for t in tools.tools]
            print("工具:", names)


if __name__ == "__main__":
    anyio.run(main, sys.argv[1])
