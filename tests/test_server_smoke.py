"""End-to-end smoke test: spawn the real server (null backend) over stdio
and speak MCP JSON-RPC to it — initialize, tools/list, tools/call."""

import json
import os
import subprocess
import sys


def rpc(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        assert line, "server closed stdout unexpectedly"
        data = json.loads(line)
        if data.get("id") == msg.get("id"):
            return data


def test_server_stdio_roundtrip():
    env = dict(os.environ, GAMEPAD_BACKEND="null", GAMEPAD_PROFILE="chiaki-ng")
    proc = subprocess.Popen(
        [sys.executable, "-m", "gamepad_mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        init = rpc(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "smoke", "version": "0"}},
        })
        assert "result" in init, init
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) + "\n")
        proc.stdin.flush()

        tools = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tools["result"]["tools"]}
        assert {"press", "hold_stick", "release_all", "status"} <= names

        call = rpc(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "press",
                       "arguments": {"button": "cross", "hold_ms": 10}},
        })
        assert "result" in call, call
        text = call["result"]["content"][0]["text"]
        assert "return" in text  # Cross maps to Return in the Chiaki profile
    finally:
        proc.terminate()
        proc.wait(timeout=10)
