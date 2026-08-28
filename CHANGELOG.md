# Changelog

Notable changes to BlockClock Connect. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- The feeder no longer stops for good when the clock fails to answer a
  push. A transport error now costs a single cycle: it is logged, a
  bounded (interruptible) backoff applies, and the rotation resumes.
  A reply the device sends mid-repaint that is not a valid HTTP status
  line is treated like the other repaint-stall replies, as accepted.

## [0.5.0]

### Added
- **Cross-source spread**: a configurable price frame comparing any two
  exchanges you pick (Kraken vs CoinGecko by default), in the currency you
  choose - the general form of a regional or venue premium.
- **Gold/silver reference currency**: show metal spot in AUD/oz, EUR/oz and any
  other currency, not only USD.
- **Wider forex pairs**: pick from the full set of ECB currencies for the base
  and quote, not just a handful.
- **Sunset time** frame now shows the local sunset clock time with a choosable
  2-character label (Sunset / Sundown / Dusk, or none), e.g. `SS18:42`.
- **On-chain cohort analytics** (bitview.space, keyless): short- and long-term
  holder MVRV and cost basis, AVIV ratio, liveliness and the thermocap
  multiple - the checkonchain-style metrics, no chart-scraping needed.
- **Compact environmental codes** in the large slots, with the code aligned
  left and reading aligned right (`TEMP 21`, `UV    7`, `WIND 18`, `HUM  64`,
  `RAIN 80`, `PM    6`, `AQ   22`, `MOON 82`), so each reading is identifiable
  without relying on the small corner labels.

### Changed
- The old AU-premium and Bitaroo-spread frames are replaced by the single
  configurable cross-source spread above; saved rotations migrate in place.

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
