"""Macro & markets: forex (ECB via Frankfurter), gold/silver spot (gold-api),
the US national debt (Treasury fiscaldata) - all keyless - plus FRED series
(S&P 500 level, US 10-year yield) behind a free API key. Keyed frames are
honestly UNAVAILABLE without the key: nothing is faked.

The FRED key is a secret like any other: header-free but query-string, so it
goes through _http_redacted (which never logs URLs) and is redacted to
fred_api_key_set / _hint in every API response.
"""

import urllib.parse

from clock import LED_AMBER, LED_RED, LED_WARM_WHITE, show_number_path
from sources import (CURRENCIES, _cached, _frame, _http_json, _http_redacted,
                     is_offline, register_source)

GOLD_API = "https://api.gold-api.com/price"
FRANKFURTER_API = "https://api.frankfurter.dev/v1/latest"
TREASURY_API = ("https://api.fiscaldata.treasury.gov/services/api/"
                "fiscal_service/v2/accounting/od/debt_to_penny")
FRED_API = "https://api.stlouisfed.org/fred/series/observations"

SYNTHETIC = {"XAU": 4332.60, "XAG": 52.10, "forex": 1.4204,
             "debt_t": 39.89, "SP500": 6890.0, "DGS10": 4.2}


def metal_spot(symbol):
    """USD spot per oz for XAU / XAG from gold-api.com (keyless, no signup).
    Newer/smaller service - degrade gracefully if it vanishes."""
    if is_offline():
        return SYNTHETIC.get(symbol)

    def fetch():
        d = _http_json(f"{GOLD_API}/{symbol}")
        try:
            return float(d["price"])
        except (TypeError, KeyError, ValueError):
            return None
    return _cached(f"metal:{symbol}", 600, fetch)


def forex_rate(base, quote):
    """ECB reference rate via Frankfurter (weekday-daily; cached an hour)."""
    if base == quote:
        return None
    if is_offline():
        return SYNTHETIC["forex"]

    def fetch():
        d = _http_json(FRANKFURTER_API + "?" + urllib.parse.urlencode(
            {"base": base, "symbols": quote}))
        try:
            return float(d["rates"][quote])
        except (TypeError, KeyError, ValueError):
            return None
    return _cached(f"fx:{base}:{quote}", 3600, fetch)


def us_debt_trillions():
    """Total US public debt in $T (official Treasury, business-daily)."""
    if is_offline():
        return SYNTHETIC["debt_t"]

    def fetch():
        d = _http_json(TREASURY_API + "?" + urllib.parse.urlencode(
            {"sort": "-record_date", "page[size]": 1}))
        try:
            return float(d["data"][0]["tot_pub_debt_out_amt"]) / 1e12
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    return _cached("us_debt", 21_600, fetch)


def fred_latest(series, api_key):
    """Latest observation of a FRED series, or None. The key rides in the
    query string, so this MUST use the redacted fetcher (no URL logging)."""
    api_key = str(api_key or "").strip()
    if not api_key:
        return None  # keyed frame honestly unavailable - no HTTP attempted
    if is_offline():
        return SYNTHETIC.get(series)

    def fetch():
        url = FRED_API + "?" + urllib.parse.urlencode(
            {"series_id": series, "api_key": api_key, "file_type": "json",
             "sort_order": "desc", "limit": 5})
        d, _why = _http_redacted(url)
        try:
            for obs in d["observations"]:
                if obs.get("value") not in (None, "", "."):
                    return float(obs["value"])
        except (TypeError, KeyError, ValueError):
            pass
        return None
    return _cached(f"fred:{series}", 3600, fetch)


def _fmt_rate(v):
    """Fit a rate/level into the 7 slots with honest precision."""
    if v < 10:
        return f"{v:.4f}"
    if v < 1000:
        return f"{v:.2f}"
    return str(int(round(v)))


def _macro_frames(options, wanted=None):
    def want(fid):
        return wanted is None or fid in wanted

    frames = []
    add = frames.append

    if want("forex"):
        base = options.get("forex_base") or "USD"
        quote = options.get("forex_quote") or "AUD"
        rate = forex_rate(base, quote)
        if rate:
            val = _fmt_rate(rate)
            add(_frame("forex",
                       show_number_path(val, tl=f"1 {base} in", br=quote),
                       number=val, color=LED_WARM_WHITE))
    if want("gold_price"):
        xau = metal_spot("XAU")
        if xau:
            val = f"{xau:.1f}" if xau < 10_000 else str(int(round(xau)))
            add(_frame("gold_price",
                       show_number_path(val, tl="gold", br="USD / oz"),
                       number=val, color=LED_AMBER))
    if want("silver_price"):
        xag = metal_spot("XAG")
        if xag and xag < 10_000:
            val = f"{xag:.2f}"
            add(_frame("silver_price",
                       show_number_path(val, tl="silver", br="USD / oz"),
                       number=val, color=LED_WARM_WHITE))
    if want("us_debt"):
        debt = us_debt_trillions()
        if debt:
            val = f"{debt:.2f}"
            add(_frame("us_debt",
                       show_number_path(val, tl="US debt", br="trillion USD"),
                       number=val, color=LED_RED))

    key = options.get("fred_api_key")
    if want("spx_index"):
        spx = fred_latest("SP500", key)
        if spx and spx < 10_000_000:
            add(_frame("spx_index",
                       show_number_path(int(round(spx)), tl="S&P 500",
                                        br="index"),
                       number=int(round(spx)), color=LED_WARM_WHITE))
    if want("us_10y"):
        y = fred_latest("DGS10", key)
        if y is not None and 0 <= y < 100:
            val = f"{y:.2f}"
            add(_frame("us_10y",
                       show_number_path(val, tl="US 10 year", br="% yield"),
                       number=val, color=LED_WARM_WHITE))
    return frames


register_source(
    "macro",
    label="Macro & markets",
    category="macro",
    description="Forex (ECB official), gold & silver spot, and the US "
                "national debt - all keyless. S&P 500 and the US 10-year "
                "yield need a free FRED API key (fred.stlouisfed.org) and "
                "stay off without one.",
    frames=[
        ("forex", "Forex rate"),
        ("gold_price", "Gold spot (USD/oz)"),
        ("silver_price", "Silver spot (USD/oz)"),
        ("us_debt", "US national debt ($T)"),
        ("spx_index", "S&P 500 [FRED key]"),
        ("us_10y", "US 10Y yield [FRED key]"),
    ],
    builder=_macro_frames,
    options_schema={
        "forex_base": {"type": "select", "label": "Forex: base",
                       "choices": CURRENCIES, "default": "USD"},
        "forex_quote": {"type": "select", "label": "Forex: quote",
                        "choices": CURRENCIES, "default": "AUD"},
        "fred_api_key": {"type": "password",
                         "label": "FRED API key (free, optional)",
                         "default": ""},
    },
    secrets=("fred_api_key",),
)
