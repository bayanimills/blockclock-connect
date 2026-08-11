# Changelog

Notable changes to BlockClock Connect. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.4.1]

### Added
- Home-screen widget (four-stats: BTC price, block height, fees, clock status).

### Fixed
- Fresh-install data directory ownership and reachability of the API for external assistants.

## [0.4.0]

### Added
- Optional, off-by-default **AI assistant / API control**: enable it to mint a
  `bcc_` bearer token, then drive the clock via the token-gated `/agent/*`
  endpoints (state, frames, show text/number, show frame, replace rotation).
- **OpenAPI 3.1 schema** of the agent endpoints at `/openapi.json`, for OpenAI
  function-calling and other tool importers.
- Bundled **MCP stdio server** (`mcp/blockclock_mcp.py`, pure stdlib, runs
  client-side) for Claude Desktop, Claude Code and other MCP clients.

## [0.3.1]

### Fixed
- Space & time stats.

### Added
- Weather city **type-to-search** (typeahead).
- BLOCKCLOCK-style preview font (embedded, self-contained).
- Units shown on the clock face for hashrate/fees.
- "Request a feed" link in the app footer.

## [0.3.0]

### Added
- **Bitaroo** and **Peach** price sources, with live AU exchange premium.
- On-chain **analytics** via BRK (MVRV, NUPL, sats-per-USD, realized price and
  friends).

## [0.2.0]

### Added
- Full stat library: multi-exchange price, network/on-chain, weather, macro,
  space & time, novelty stats.
- Per-stat **preview with backlight colour** ("preview with lights") in the UI.
- Connect your **own node**: your own mempool instance, Bitcoin Core, LND.
- Optional **merchant** feeds (Shopify / BTCPay).

## [0.1.0]

### Added
- First release: discover a BLOCKCLOCK on the network (or enter its IP) and
  connect to it.
- BTC price, Bitcoin network stats and weather feeds, with rotation control.
