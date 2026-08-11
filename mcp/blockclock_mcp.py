#!/usr/bin/env python3
"""BlockClock Connect - MCP server (stdio, pure stdlib, no dependencies).

Runs on YOUR machine (not in the app's Docker image) and lets an MCP client
(Claude Desktop, Claude Code, or anything speaking MCP over stdio) drive your
BLOCKCLOCK through the app's token-gated /agent/* HTTP API.

Setup (see mcp/README.md for the full walkthrough):
  1. In the BlockClock Connect app, open Advanced > "AI assistant / API
     access", turn it ON and copy the token.
  2. Export two environment variables:
       BLOCKCLOCK_URL    e.g. http://umbrel.local:4200
       BLOCKCLOCK_TOKEN  the bcc_... token you copied
  3. Register this file with your MCP client, e.g.
       claude mcp add blockclock \
         -e BLOCKCLOCK_URL=http://umbrel.local:4200 \
         -e BLOCKCLOCK_TOKEN=bcc_... \
         -- python /path/to/blockclock_mcp.py

Tools exposed: get_state, list_frames, show_text, show_number, show_frame,
set_rotation. Every push respects the clock's one-write-per-~65s rate limit
(the app queues it and reports an ETA - this server never bypasses that).

This is a deliberately minimal JSON-RPC 2.0 / MCP stdio loop (protocol
2024-11-05, newline-delimited JSON) so it runs on any Python 3.8+ with no
`pip install` at all.
"""

import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "blockclock-connect", "version": "1.0.0"}


# --------------------------------------------------------------------------- #
# HTTP -> the app's /agent/* API
# --------------------------------------------------------------------------- #

def _base_url():
    return (os.environ.get("BLOCKCLOCK_URL") or "").strip().rstrip("/")


def _token():
    return (os.environ.get("BLOCKCLOCK_TOKEN") or "").strip()


def call_api(method, path, body=None):
    """Call the app. Returns (parsed-json, error-string-or-None)."""
    base, token = _base_url(), _token()
    if not base:
        return None, ("BLOCKCLOCK_URL is not set. Set it to the app's base "
                      "URL, e.g. http://umbrel.local:4200")
    if not token:
        return None, ("BLOCKCLOCK_TOKEN is not set. In the BlockClock "
                      "Connect app open Advanced > 'AI assistant / API "
                      "access', turn it on and copy the token.")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "blockclock-mcp/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("error")
        except Exception:
            msg = None
        if e.code == 401:
            msg = msg or "Wrong token."
        elif e.code == 403:
            msg = msg or ("API access is switched off in the app - the "
                          "owner has to turn it on under Advanced.")
        return None, f"HTTP {e.code}: {msg or 'request rejected'}"
    except Exception as e:
        return None, (f"Could not reach the app at {base} "
                      f"({type(e).__name__}). Is BLOCKCLOCK_URL right and "
                      "the app running?")


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

TOOLS = [
    {
        "name": "get_state",
        "description": (
            "Current BLOCKCLOCK state: whether a clock is connected and "
            "being driven, the frame on its display right now, seconds "
            "until the next write window, and the rotation of frame ids. "
            "The clock accepts about one display change per 65 seconds."),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
    {
        "name": "list_frames",
        "description": (
            "List every ready-made frame (stat) the app can show - Bitcoin "
            "price, block height, fees, weather and more. Each has a stable "
            "id for show_frame/set_rotation; 'available' means its data "
            "source is enabled and can render right now."),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
    {
        "name": "show_text",
        "description": (
            "Put a short piece of text on the clock NOW (queued into the "
            "next ~65s write window, then the normal rotation resumes). "
            "The display has 7 character slots, shown uppercase. tl/br are "
            "small caption lines (about 13 characters) above/below."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "maxLength": 7,
                         "description": "Up to 7 characters."},
                "tl": {"type": "string", "maxLength": 13,
                       "description": "Small top caption (optional)."},
                "br": {"type": "string", "maxLength": 13,
                       "description": "Small bottom caption (optional)."},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "show_number",
        "description": (
            "Put a number on the clock NOW (queued into the next ~65s "
            "write window, then the rotation resumes). Up to 7 digits; a "
            "'pair' like BTC/USD occupies the first slot so only 6 digits "
            "fit next to it. 'sym' is an optional one-character currency "
            "symbol. tl/br are small captions (about 13 characters)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {"type": ["number", "string"],
                           "description": "The number to show."},
                "pair": {"type": "string",
                         "description": "Optional X/Y unit in the first "
                                        "slot, e.g. BTC/USD or SAT/VB."},
                "sym": {"type": "string", "maxLength": 1,
                        "description": "Optional symbol, e.g. $."},
                "tl": {"type": "string", "maxLength": 13},
                "br": {"type": "string", "maxLength": 13},
            },
            "required": ["number"],
            "additionalProperties": False,
        },
    },
    {
        "name": "show_frame",
        "description": (
            "Show one ready-made frame from the library NOW by its id "
            "(from list_frames), queued into the next ~65s write window; "
            "the rotation resumes afterwards."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "frame_id": {"type": "string",
                             "description": "A frame id from list_frames, "
                                            "e.g. 'btc_price'."},
            },
            "required": ["frame_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_rotation",
        "description": (
            "Replace the ordered list of frames the clock cycles through "
            "(one per ~65s). Ids come from list_frames. Persists and takes "
            "effect on the clock's next write."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "frames": {"type": "array", "minItems": 1,
                           "items": {"type": "string"},
                           "description": "Ordered frame ids."},
            },
            "required": ["frames"],
            "additionalProperties": False,
        },
    },
]


def run_tool(name, args):
    """Dispatch one tool call. Returns (text, is_error)."""
    args = args or {}
    if name == "get_state":
        out, err = call_api("GET", "/agent/state")
    elif name == "list_frames":
        out, err = call_api("GET", "/agent/frames")
    elif name == "show_text":
        body = {"text": args.get("text")}
        for k in ("tl", "br"):
            if args.get(k):
                body[k] = args[k]
        out, err = call_api("POST", "/agent/show", body)
    elif name == "show_number":
        body = {"number": args.get("number")}
        for k in ("pair", "sym", "tl", "br"):
            if args.get(k):
                body[k] = args[k]
        out, err = call_api("POST", "/agent/show", body)
    elif name == "show_frame":
        out, err = call_api("POST", "/agent/frame",
                            {"frame_id": args.get("frame_id")})
    elif name == "set_rotation":
        out, err = call_api("POST", "/agent/rotation",
                            {"frames": args.get("frames")})
    else:
        return f"Unknown tool '{name}'", True
    if err:
        return err, True
    return json.dumps(out, indent=2), False


# --------------------------------------------------------------------------- #
# Minimal MCP stdio loop (JSON-RPC 2.0, newline-delimited)
# --------------------------------------------------------------------------- #

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def reply(mid, result):
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def reply_error(mid, code, message):
    send({"jsonrpc": "2.0", "id": mid,
          "error": {"code": code, "message": message}})


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        reply(mid, {
            "protocolVersion": params.get("protocolVersion",
                                          PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method == "ping":
        reply(mid, {})
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        text, is_error = run_tool(params.get("name"),
                                  params.get("arguments"))
        reply(mid, {"content": [{"type": "text", "text": text}],
                    "isError": is_error})
    elif method and method.startswith("notifications/"):
        pass  # notifications get no reply
    elif mid is not None:
        reply_error(mid, -32601, f"Method not found: {method}")


def main():
    if not _base_url() or not _token():
        print("blockclock-mcp: warning: BLOCKCLOCK_URL and/or "
              "BLOCKCLOCK_TOKEN are not set; tool calls will explain "
              "how to fix this.", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # not JSON; ignore rather than crash the session
        try:
            handle(msg)
        except Exception as e:  # never take the whole session down
            if msg.get("id") is not None:
                reply_error(msg.get("id"), -32603,
                            f"internal error: {type(e).__name__}")


if __name__ == "__main__":
    main()
