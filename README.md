# BlockClock Connect

**Drive your Coinkite BLOCKCLOCK with the data you care about, from one simple
web page on your Umbrel.**

Point a Coinkite **BLOCKCLOCK** (mini or micro) at the stats you want: find your
clock on the network, pick the feeds to show (each previewed exactly as it looks
on the device, backlight colour and all), arrange the rotation, set when each
shows, and play it live.

## Features

- **Bitcoin price** from your choice of exchange (Coinbase, Kraken, Bitstamp,
  Bitaroo, Peach…) in your own currency, plus sats-per-unit, Moscow time and the
  local exchange premium.
- **Bitcoin network & on-chain**: block height, fees, hashrate, halving, mempool,
  Lightning, and analytics (MVRV, NUPL, sats-per-USD, realized price…).
- **Your own node**: point the network stats at your own (or anyone's) mempool
  instance, or connect Bitcoin Core / LND.
- **Weather, macro, space & time**, novelty stats, and optional **merchant**
  feeds (Shopify / BTCPay).
- **AI / agent control** (optional, off by default): turn it on to get a token
  and drive the clock from Claude, OpenAI or any MCP client. See [`mcp/`](mcp/).

Every key or password you add stays on your Umbrel and is only used to read your
own data. The app talks to the clock over your own network using the clock's
built-in local API, and the served web UI is fully self-contained (no external
fonts, scripts or CDNs).

## Screenshots

App-store screenshots live in the store repo's
[`sba-blockclock/gallery/`](https://github.com/bayanimills/sba-umbrel-store/tree/main/sba-blockclock/gallery)
folder (also shown on the app's page in the Umbrel App Store).

## Install (via the SBA Umbrel Store)

BlockClock Connect is distributed through the
[**SBA Umbrel App Store**](https://github.com/bayanimills/sba-umbrel-store):

1. On your umbrelOS home screen, open the **App Store**.
2. Click the **⋯** (three-dots) menu → **Community App Stores**.
3. Paste `https://github.com/bayanimills/sba-umbrel-store` and click **Add**.
4. Open the SBA store and install **BlockClock Connect**.

## Develop

```bash
docker build -t ghcr.io/bayanimills/blockclock-connect:dev .
docker run -d --restart unless-stopped -p 4200:4200 -v "$PWD/data:/data" \
  ghcr.io/bayanimills/blockclock-connect:dev
# open http://localhost:4200
```

Pure-python (Flask + waitress; external data via stdlib `urllib`), so the same
image builds unchanged for `linux/amd64` and `linux/arm64` (Umbrel). Runs as
user 1000, data persists in the mounted `/data` volume.

Run the offline test suite (no network, synthetic data):

```bash
pip install flask waitress
python app.py --selfcheck
```

CI runs the selfcheck on every push/PR, and a tag push (`v*`) builds and
publishes the multi-arch image to `ghcr.io/bayanimills/blockclock-connect`.

## Agent / API control

Enable "AI assistant / API access" in the app to mint a bearer token, then use
the documented `/agent/*` endpoints, the OpenAPI 3.1 schema at `/openapi.json`,
or the bundled MCP server to drive the clock from an LLM. Setup guide and full
endpoint reference: [`mcp/README.md`](mcp/README.md).

## Contributing

Issues and ideas are welcome. **Missing a stat?**
[Request a feed](https://github.com/bayanimills/blockclock-connect/issues/new?labels=feed-request)
using the feed-request template (what stat, where the data comes from, what it
should look like on the 7-character face). Bug reports have a template too.

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

[MIT](LICENSE) © 2026 Bayani Mills.

The web UI embeds a subset of **JetBrains Mono ExtraBold** (renamed
"BC Display") for the display preview, used under the
[SIL Open Font License 1.1](licenses/JetBrains-Mono-OFL.txt). See
[NOTICE](NOTICE) for third-party credits. BLOCKCLOCK is a product of Coinkite
Inc.; this is an independent companion app, not affiliated with Coinkite.

---
<sub><i>Vires in numeris.</i></sub>
