"""Bitcoin price - a PROVIDER source with a pluggable exchange.

One source, many exchanges: the `exchange` option picks where the quote comes
from (Coinbase stays the default - existing configs keep working unchanged).
Every exchange is KEYLESS - including Bitaroo, which moved to its public
/trade/market-data endpoint (the old /v1/market/ticker path 403s without a
token; a bitaroo_api_key saved by an older version is simply ignored).

Frames: the price itself, 24h change % (where the exchange serves one),
sats-per-unit, Moscow time (sats per USD, maximum Bitcoin culture), the
price in gold ounces (via gold-api XAU spot), and a configurable cross-source
spread - the % gap between any two exchanges you pick (Kraken vs CoinGecko by
default), the honest way to watch a regional or venue premium.
"""

import urllib.parse

from clock import (LED_AMBER, LED_BTC_ORANGE, LED_GREEN, LED_RED,
                   show_number_path)
from sources import (CACHE_TTL_S, COINBASE_API, CURRENCIES, _cached, _frame,
                     _http_json, is_offline, register_source)

SYNTHETIC_PRICES = {"USD": 108_000.0, "AUD": 165_000.0, "EUR": 99_000.0,
                    "GBP": 85_000.0, "JPY": 16_400_000.0}
SYNTHETIC_CHANGE = 1.2
# a parsed Bitaroo market-data snapshot: mid 165,050 vs the synthetic AUD
# spot 165,000 -> a +0.03% premium; (ask-bid)/mid -> a 0.18% spread
SYNTHETIC_BITAROO = {"last": 165_050.0, "high": 167_900.0, "low": 163_800.0,
                     "change_pct": -1.1, "bid": 164_900.0, "ask": 165_200.0}

# Exchange metadata: which currencies each serves and whether it serves (or
# lets us compute) a 24h change. `ccys` drives both the UI (constrain the
# picker) and the builder (skip an unsupported pair, never silently switch
# currency). Every exchange is keyless.
_MEMPOOL_CCYS = ["USD", "EUR", "GBP", "CAD", "CHF", "AUD", "JPY"]
EXCHANGES = {
    "coinbase":        {"label": "Coinbase", "ccys": list(CURRENCIES),
                        "change": True},
    "mempool":         {"label": "mempool.space", "ccys": _MEMPOOL_CCYS,
                        "change": False},
    "kraken":          {"label": "Kraken", "ccys": _MEMPOOL_CCYS,
                        "change": True},
    "bitstamp":        {"label": "Bitstamp", "ccys": ["USD", "EUR", "GBP"],
                        "change": True},
    "btcmarkets":      {"label": "BTC Markets (AU)", "ccys": ["AUD"],
                        "change": True},
    "coinjar":         {"label": "CoinJar (AU)", "ccys": ["AUD"],
                        "change": True},
    "blockchain_info": {"label": "blockchain.info", "ccys": list(CURRENCIES),
                        "change": False},
    "bitfinex":        {"label": "Bitfinex",
                        "ccys": ["USD", "EUR", "GBP", "JPY"], "change": True},
    "gemini":          {"label": "Gemini",
                        "ccys": ["USD", "EUR", "GBP", "SGD"], "change": False},
    "binance":         {"label": "Binance (USDT)", "ccys": ["USD"],
                        "change": True,
                        "note": "Quotes BTC/USDT, not USD; geo-blocked from "
                                "US IPs."},
    "coingecko":       {"label": "CoinGecko", "ccys": list(CURRENCIES),
                        "change": True},
    "bitaroo":         {"label": "Bitaroo (AU)", "ccys": ["AUD"],
                        "change": True},
    "peach":           {"label": "Peach Bitcoin (P2P)",
                        "ccys": ["AUD", "EUR", "CHF"], "change": False,
                        "note": "Peach's P2P market rate (their offer "
                                "index), not a traded last price."},
}

# Short codes for the cross-source spread frame's small labels (the device's
# tl/br lines are narrow, so "KRK v CGK" reads far better than the full names).
EXCHANGE_CODE = {
    "coinbase": "CB", "mempool": "MEM", "kraken": "KRK", "bitstamp": "BST",
    "btcmarkets": "BTM", "coinjar": "CJ", "blockchain_info": "BCI",
    "bitfinex": "BFX", "gemini": "GEM", "binance": "BNB", "coingecko": "CGK",
    "bitaroo": "BTR", "peach": "PCH",
}


# --------------------------------------------------------------------------- #
# Quote fetchers. Each returns {"price": float, "change": float|None} or None.
# All keyless.
# --------------------------------------------------------------------------- #

def _q_coinbase(ccy, options):
    d = _http_json(f"{COINBASE_API}/BTC-{urllib.parse.quote(ccy)}/spot",
                   timeout=15)
    try:
        price = float(d["data"]["amount"])
    except (TypeError, KeyError, ValueError):
        return None
    change = None
    # 24h change from Coinbase Exchange stats (open vs last); only the major
    # pairs exist there - a miss just means no change frame.
    s = _http_json(f"https://api.exchange.coinbase.com/products/BTC-{ccy}/stats")
    try:
        o, last = float(s["open"]), float(s["last"])
        if o:
            change = (last - o) / o * 100
    except (TypeError, KeyError, ValueError):
        pass
    return {"price": price, "change": change}


def _q_mempool(ccy, options):
    d = _http_json("https://mempool.space/api/v1/prices")
    try:
        return {"price": float(d[ccy]), "change": None}
    except (TypeError, KeyError, ValueError):
        return None


def _q_kraken(ccy, options):
    d = _http_json(f"https://api.kraken.com/0/public/Ticker?pair=XBT{ccy}")
    try:
        v = next(iter(d["result"].values()))
        last, opn = float(v["c"][0]), float(v["o"])
        change = (last - opn) / opn * 100 if opn else None
        return {"price": last, "change": change}
    except (TypeError, KeyError, IndexError, ValueError, StopIteration):
        return None


def _q_bitstamp(ccy, options):
    d = _http_json(f"https://www.bitstamp.net/api/v2/ticker/btc{ccy.lower()}/")
    try:
        price = float(d["last"])
    except (TypeError, KeyError, ValueError):
        return None
    change = None
    try:
        if d.get("percent_change_24") is not None:
            change = float(d["percent_change_24"])
        elif d.get("open") and float(d["open"]):
            change = (price - float(d["open"])) / float(d["open"]) * 100
    except (TypeError, ValueError):
        pass
    return {"price": price, "change": change}


def _q_btcmarkets(ccy, options):
    d = _http_json("https://api.btcmarkets.net/v3/markets/BTC-AUD/ticker")
    try:
        change = (float(d["pricePct24h"])
                  if d.get("pricePct24h") is not None else None)
        return {"price": float(d["lastPrice"]), "change": change}
    except (TypeError, KeyError, ValueError):
        return None


def _q_coinjar(ccy, options):
    d = _http_json("https://data.exchange.coinjar.com/products/BTCAUD/ticker")
    try:
        last = float(d["last"])
        prev = float(d.get("prev_close") or 0)
        change = (last - prev) / prev * 100 if prev else None
        return {"price": last, "change": change}
    except (TypeError, KeyError, ValueError):
        return None


def _q_blockchain_info(ccy, options):
    d = _http_json("https://blockchain.info/ticker")
    try:
        return {"price": float(d[ccy]["last"]), "change": None}
    except (TypeError, KeyError, ValueError):
        return None


def _q_bitfinex(ccy, options):
    d = _http_json(f"https://api-pub.bitfinex.com/v2/ticker/tBTC{ccy}")
    try:  # [..., 5]=daily change relative, [6]=last price
        change = float(d[5]) * 100 if d[5] is not None else None
        return {"price": float(d[6]), "change": change}
    except (TypeError, IndexError, ValueError):
        return None


def _q_gemini(ccy, options):
    d = _http_json(f"https://api.gemini.com/v1/pubticker/btc{ccy.lower()}")
    try:
        return {"price": float(d["last"]), "change": None}
    except (TypeError, KeyError, ValueError):
        return None


def _q_binance(ccy, options):
    d = _http_json("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT")
    try:
        change = (float(d["priceChangePercent"])
                  if d.get("priceChangePercent") is not None else None)
        return {"price": float(d["lastPrice"]), "change": change}
    except (TypeError, KeyError, ValueError):
        return None


def _q_coingecko(ccy, options):
    lc = ccy.lower()
    d = _http_json("https://api.coingecko.com/api/v3/simple/price?"
                   + urllib.parse.urlencode({"ids": "bitcoin",
                                             "vs_currencies": lc,
                                             "include_24hr_change": "true"}))
    try:
        b = d["bitcoin"]
        change = b.get(f"{lc}_24h_change")
        return {"price": float(b[lc]),
                "change": float(change) if change is not None else None}
    except (TypeError, KeyError, ValueError):
        return None


BITAROO_MD = "https://api.bitaroo.com.au/trade/market-data/btcaud"


def _best_price(side, pick):
    """Best price out of one order-book side. Rows may be dicts
    ({"price": ...}) or [price, amount] pairs; prices may be strings.
    pick=max for bids, pick=min for asks (order-independent)."""
    prices = []
    for row in side or []:
        raw = row.get("price") if isinstance(row, dict) else \
            (row[0] if isinstance(row, (list, tuple)) and row else None)
        try:
            prices.append(float(raw))
        except (TypeError, ValueError):
            continue
    return pick(prices) if prices else None


def bitaroo_market():
    """Bitaroo's KEYLESS market-data snapshot (the endpoint their own
    homepage calls; the old /v1/market/ticker 403s), parsed down to the
    handful of numbers the frames need. The raw payload carries the FULL
    order book (tens of KB), so only this parsed dict is ever cached (~60s,
    stale-tolerant). Dict of last/high/low/change_pct/bid/ask (each may be
    None) or None entirely."""
    if is_offline():
        return dict(SYNTHETIC_BITAROO)

    def fetch():
        d = _http_json(BITAROO_MD, timeout=15)
        if not isinstance(d, dict):
            return None
        stats = d.get("dailyStats") or {}
        out = {}
        for key, field in (("last", "lastPrice"), ("high", "high"),
                           ("low", "low"),
                           ("change_pct", "changePercentage")):
            try:
                out[key] = float(stats.get(field))
            except (TypeError, ValueError):
                out[key] = None
        book = d.get("orderBook") or {}
        out["bid"] = _best_price(book.get("buy", book.get("bids")), max)
        out["ask"] = _best_price(book.get("sell", book.get("asks")), min)
        if out["last"] is None and not (out["bid"] and out["ask"]):
            return None
        return out
    return _cached("bitaroo_md", 60, fetch)


def _q_bitaroo(ccy, options):
    # KEYLESS via the public market-data endpoint. A bitaroo_api_key saved by
    # an older version is ignored (kept in config, still redacted).
    md = bitaroo_market()
    if not isinstance(md, dict):
        return None
    price = md.get("last")
    if price is None and md.get("bid") and md.get("ask"):
        price = (md["bid"] + md["ask"]) / 2
    if not price:
        return None
    return {"price": float(price), "change": md.get("change_pct")}


def _q_peach(ccy, options):
    d = _http_json("https://api.peachbitcoin.com/v1/market/price/BTC"
                   + urllib.parse.quote(ccy))
    try:
        return {"price": float(d["price"]), "change": None}
    except (TypeError, KeyError, ValueError):
        return None


_FETCHERS = {
    "coinbase": _q_coinbase, "mempool": _q_mempool, "kraken": _q_kraken,
    "bitstamp": _q_bitstamp, "btcmarkets": _q_btcmarkets,
    "coinjar": _q_coinjar, "blockchain_info": _q_blockchain_info,
    "bitfinex": _q_bitfinex, "gemini": _q_gemini, "binance": _q_binance,
    "coingecko": _q_coingecko, "bitaroo": _q_bitaroo, "peach": _q_peach,
}


def get_quote(exchange, ccy, options=None):
    """Cached quote from the chosen exchange, or None (unsupported pair, or
    the exchange is down - callers skip)."""
    if is_offline():
        return {"price": SYNTHETIC_PRICES.get(ccy, 108_000.0),
                "change": SYNTHETIC_CHANGE}
    spec = EXCHANGES.get(exchange)
    if not spec or ccy not in spec["ccys"]:
        return None
    return _cached(f"px:{exchange}:{ccy}", CACHE_TTL_S,
                   lambda: _FETCHERS[exchange](ccy, options))


def btc_price(ccy):
    """The trusted keyless spot price (Coinbase), kept as the shared helper
    other sources use for fiat->sats conversion. Unchanged behaviour."""
    if is_offline():
        return SYNTHETIC_PRICES.get(ccy, 108_000.0)

    def fetch():
        d = _http_json(f"{COINBASE_API}/BTC-{urllib.parse.quote(ccy)}/spot",
                       timeout=15)
        try:
            return float(d["data"]["amount"])
        except (TypeError, KeyError, ValueError):
            return None
    return _cached(f"price:{ccy}", CACHE_TTL_S, fetch)


def sats_per_unit(price):
    """sats per 1 unit of fiat = 100,000,000 / (fiat per BTC)."""
    if not price:
        return None
    return int(round(100_000_000 / price))


def _usd_price(exchange, options):
    """A USD price for Moscow time / gold maths: the chosen exchange if it
    serves USD, else the trusted Coinbase spot."""
    spec = EXCHANGES.get(exchange) or {}
    if "USD" in (spec.get("ccys") or []):
        q = get_quote(exchange, "USD", options)
        if q and q.get("price"):
            return q["price"]
    return btc_price("USD")


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #

def _price_frames(options, wanted=None):
    exchange = options.get("exchange") or "coinbase"
    if exchange not in EXCHANGES:
        exchange = "coinbase"
    ccy = options.get("currency", "USD")
    unit = "USDT" if exchange == "binance" else ccy

    def want(fid):
        return wanted is None or fid in wanted

    frames = []
    quote = get_quote(exchange, ccy, options)
    price = quote.get("price") if quote else None
    if price and want("btc_price"):
        amt = int(round(price))
        digits = str(amt)
        if len(digits) <= 6:
            # native look: pair in slot 0 + price. No sym: a 6-digit price +
            # pair fills all slots and the device silently drops the symbol,
            # so appearance stayed consistent without it.
            frames.append(_frame(
                "btc_price",
                show_number_path(amt, pair=f"BTC/{unit}", tl="Bitcoin price"),
                number=amt, pair=f"BTC/{unit}", color=LED_BTC_ORANGE))
        elif len(digits) <= 7:
            # the pair would eat slot 0 and truncate the price; move it to tl
            frames.append(_frame(
                "btc_price",
                show_number_path(amt, tl=f"BTC/{unit} price"),
                number=amt, color=LED_BTC_ORANGE))
        else:
            # e.g. JPY: 8+ digits never fit -> show thousands, honestly labelled
            frames.append(_frame(
                "btc_price",
                show_number_path(amt // 1000, tl=f"BTC/{unit}", br="x 1000"),
                number=amt // 1000, color=LED_BTC_ORANGE))
    if price and want("sats_per_unit"):
        spu = sats_per_unit(price)
        if spu is not None:
            frames.append(_frame(
                "sats_per_unit",
                show_number_path(spu, tl="sats per 1", br=ccy),
                number=spu, color=LED_BTC_ORANGE))
    if want("price_change"):
        change = quote.get("change") if quote else None
        if change is not None and abs(change) < 1000:
            val = f"{change:.1f}"
            frames.append(_frame(
                "price_change",
                show_number_path(val, tl="BTC 24h", br="% change"),
                number=val,
                color=LED_GREEN if change >= 0 else LED_RED))
    if want("moscow_time"):
        usd = price if ccy == "USD" and price else _usd_price(exchange, options)
        spu = sats_per_unit(usd)
        if spu is not None and spu <= 9_999_999:
            frames.append(_frame(
                "moscow_time",
                show_number_path(spu, tl="Moscow time", br="sats/USD"),
                number=spu, color=LED_BTC_ORANGE))
    if want("price_gold"):
        from sources.macro import metal_spot  # lazy: macro registers later
        usd = price if ccy == "USD" and price else _usd_price(exchange, options)
        xau = metal_spot("XAU")
        if usd and xau:
            val = f"{usd / xau:.2f}"
            if len(val) <= 7:
                frames.append(_frame(
                    "price_gold",
                    show_number_path(val, tl="BTC in gold", br="ounces"),
                    number=val, color=LED_AMBER))

    # ---- configurable cross-source spread: exchange A vs exchange B ----
    # The signed % gap (A/B - 1) between any two exchanges you pick - the
    # honest, general form of a regional/venue premium. Skips cleanly whenever
    # either leg is unavailable (unsupported pair or a down exchange).
    if want("price_compare"):
        a_id = options.get("compare_a") or "kraken"
        b_id = options.get("compare_b") or "coingecko"
        cccy = options.get("compare_currency") or "USD"
        qa = get_quote(a_id, cccy, options)
        qb = get_quote(b_id, cccy, options)
        pa = qa.get("price") if qa else None
        pb = qb.get("price") if qb else None
        if pa and pb and a_id != b_id:
            spread = (pa / pb - 1) * 100
            val = f"{spread:+.2f}"  # signed, e.g. +0.34 / -0.12
            if len(val) <= 7:
                ca = EXCHANGE_CODE.get(a_id, a_id[:3].upper())
                cb = EXCHANGE_CODE.get(b_id, b_id[:3].upper())
                frames.append(_frame(
                    "price_compare",
                    show_number_path(val, tl=f"{ca} v {cb}", br="spread %"),
                    number=val,
                    color=LED_GREEN if spread >= 0 else LED_RED))
    return frames


register_source(
    "price",
    label="Bitcoin price",
    category="price",
    description="Live BTC price from your choice of exchange (all keyless - "
                "no API keys), plus 24h change, sats per unit, Moscow time, "
                "the price in gold ounces, and a cross-source spread that "
                "compares any two exchanges you pick.",
    frames=[
        ("btc_price", "Bitcoin price"),
        ("price_change", "24h change %"),
        ("sats_per_unit", "Sats per unit"),
        ("moscow_time", "Moscow time (sats/USD)"),
        ("price_gold", "Price in gold oz"),
        ("price_compare", "Exchange spread (A vs B)"),
    ],
    builder=_price_frames,
    options_schema={
        "exchange": {"type": "select", "label": "Exchange",
                     "choices": [{"id": k, "label": v["label"],
                                  "currencies": v["ccys"],
                                  "keyed": bool(v.get("keyed")),
                                  **({"note": v["note"]} if v.get("note")
                                     else {})}
                                 for k, v in EXCHANGES.items()],
                     "default": "coinbase"},
        "currency": {"type": "select", "label": "Currency",
                     "choices": CURRENCIES, "default": "USD"},
        "compare_a": {"type": "select", "label": "Spread: exchange A",
                      "choices": [{"id": k, "label": v["label"],
                                   "currencies": v["ccys"]}
                                  for k, v in EXCHANGES.items()],
                      "default": "kraken"},
        "compare_b": {"type": "select", "label": "Spread: exchange B",
                      "choices": [{"id": k, "label": v["label"],
                                   "currencies": v["ccys"]}
                                  for k, v in EXCHANGES.items()],
                      "default": "coingecko"},
        "compare_currency": {"type": "select", "label": "Spread: currency",
                             "choices": CURRENCIES, "default": "USD"},
    },
    # DEPRECATED: Bitaroo went keyless. The key stays a declared secret so a
    # value saved by an older version keeps loading and stays redacted in
    # every API response - but nothing reads it any more.
    secrets=("bitaroo_api_key",),
)
