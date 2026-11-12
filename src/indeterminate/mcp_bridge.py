"""MCP server that lets a CLI agent (Claude Code, Codex) use the workbench's tools.

It runs as a stdio MCP server started by the CLI for one agent. Every tool call
is forwarded to the running workbench (``/api/bridge/<project>/<agent>/…``),
so the agent's permissions, the user's approvals, the project layout rules,
the event timeline and the budget all apply exactly as for API-driven agents.

    python -m indeterminate.mcp_bridge --url http://127.0.0.1:8765 --project P --agent A
    (token in $MCH_BRIDGE_TOKEN)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"


def api_name(name: str) -> str:
    return name.replace(".", "__")


def tool_name(api: str) -> str:
    return api.replace("__", ".")


class Bridge:
    def __init__(self, url: str, project: str, agent: str, token: str):
        self.base = f"{url.rstrip('/')}/api/bridge/{project}/{agent}"
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # always local, never via a proxy

    def _request(self, path: str, body: dict | None = None, timeout: float = 30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data, {"X-MCH-Bridge": self.token, "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise RuntimeError(f"工作台拒绝了请求（{exc.code}）：{detail}") from exc
        except OSError as exc:
            raise RuntimeError(f"连接不到工作台：{exc}") from exc

    def tools(self) -> list[dict]:
        return self._request("/tools")["tools"]

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        # Tool calls may wait for the user's approval: allow a long time.
        r = self._request("/call", {"name": name, "args": args}, timeout=6 * 3600)
        return r["content"], bool(r["error"])


def handle(bridge: Bridge, msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notifications need no answer
        return None
    try:
        if method == "initialize":
            client = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
            result = {"protocolVersion": client, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "mch", "title": "未定式工作台", "version": "0.1"},
                      "instructions": "这些是工作台的工具：文件、流程、实验、Git、想法与决策、子 Agent 等。所有工作都通过它们完成。"}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": api_name(t["name"]), "description": t["description"],
                                 "inputSchema": t["parameters"] or {"type": "object", "properties": {}}} for t in bridge.tools()]}
        elif method == "tools/call":
            params = msg.get("params") or {}
            content, error = bridge.call(tool_name(params.get("name", "")), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": content}], "isError": error}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持的方法：{method}"}}
    except RuntimeError as exc:
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": f"错误：{exc}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(bridge: Bridge, stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "无法解析的 JSON"}}
        else:
            reply = handle(bridge, msg)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(prog="indeterminate.mcp_bridge")
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()
    serve(Bridge(args.url, args.project, args.agent, os.environ.get("MCH_BRIDGE_TOKEN", "")))


if __name__ == "__main__":
    main()
