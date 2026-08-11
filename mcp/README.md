# Drive your BLOCKCLOCK from an AI assistant

BlockClock Connect ships an **optional, off-by-default** agent API. Once you
turn it on and hand your assistant the token, it can put text/numbers on the
clock, show any frame from the library, and change the rotation - and nothing
else. It cannot read your keys or settings, and every push still respects the
clock's one-write-per-~65-seconds rate limit.

## 1. Enable API access and copy the token

1. Open the BlockClock Connect app on your Umbrel.
2. Scroll to **Advanced · AI assistant / API access** and turn on
   *"Let an AI assistant control this clock"*.
3. Click **Reveal** (or **Copy**) to get the token (starts with `bcc_`).

Notes:

* **Off by default.** While the toggle is off, every `/agent/*` request is
  refused (403), token or not.
* **Regenerate** replaces the token; the old one stops working immediately.
  Turning access off keeps the token, so re-enabling later "just works".
* Treat the token like a password: anything holding it can change what your
  clock shows.

## 2. Set two environment variables

```
BLOCKCLOCK_URL     the app's base URL, e.g. http://umbrel.local:4200
BLOCKCLOCK_TOKEN   the bcc_... token you copied
```

(The base URL is shown next to the token in the app.)

## 3a. MCP (Claude Desktop, Claude Code, any MCP client)

`blockclock_mcp.py` in this folder is a self-contained MCP stdio server -
pure Python standard library, **no `pip install`**, Python 3.8+. It exposes:
`get_state`, `list_frames`, `show_text`, `show_number`, `show_frame`,
`set_rotation`.

**Claude Code:**

```
claude mcp add blockclock \
  -e BLOCKCLOCK_URL=http://umbrel.local:4200 \
  -e BLOCKCLOCK_TOKEN=bcc_your_token_here \
  -- python /path/to/mcp/blockclock_mcp.py
```

**Claude Desktop** - add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "blockclock": {
      "command": "python",
      "args": ["/path/to/mcp/blockclock_mcp.py"],
      "env": {
        "BLOCKCLOCK_URL": "http://umbrel.local:4200",
        "BLOCKCLOCK_TOKEN": "bcc_your_token_here"
      }
    }
  }
}
```

Then ask things like *"put GM on my blockclock"* or *"set my clock rotation
to price, fees and block height"*.

## 3b. OpenAI function-calling / other tool importers

The app serves an OpenAPI 3.1 schema of the agent endpoints at
`GET /openapi.json` (e.g. `http://umbrel.local:4200/openapi.json`). Import it
wherever OpenAPI tools are accepted (OpenAI function-calling converters,
Actions-style integrations, etc.), set the server URL to your app's base URL,
and authenticate with `Authorization: Bearer <token>`.

Remember the app usually lives on your LAN - a cloud-hosted agent can only
reach it if you deliberately expose it (behind a VPN or reverse proxy). The bundled MCP
server runs on your own machine, so it has no such problem.

## The HTTP API itself

All endpoints require `Authorization: Bearer <token>` and answer clean JSON.

| Endpoint              | What it does |
|-----------------------|--------------|
| `GET  /agent/state`   | Connection, running, current frame, seconds until next write, rotation |
| `GET  /agent/frames`  | Every frame id/label/category, with `available` / `in_rotation` flags |
| `POST /agent/show`    | One-off custom frame now: `{text}` or `{number, sym?, pair?}` + `tl?/br?` |
| `POST /agent/frame`   | One-off library frame now: `{frame_id}` |
| `POST /agent/rotation`| Replace the rotation: `{frames: [ids...]}` |

The display has 7 character slots; a `pair` (e.g. `BTC/USD`) uses the first
slot. "Now" means "in the next write window" - the response's `note`/`eta_s`
say when (the clock hard-limits writes to about one per 65 s).

This folder is not part of the Docker image - it is client-side tooling you
run wherever your assistant lives.
