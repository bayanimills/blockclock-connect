"""On-chain analytics - the BRK (Bitcoin Research Kit) series pack.

bitview.space serves BRK's on-chain series keyless as bare numbers:
GET /api/vecs/dateindex-to-<series>?from=-1 -> the latest daily value.
Everything here is USD-based and updates once a DAY, so reads are cached for
hours and stale-tolerant. A wrong/renamed series name gets an error payload
back (not a number): it fails to parse and the frame simply skips - a bad
name can never crash the loop.
"""

from clock import LED_GREEN, LED_RED, LED_WARM_WHITE, show_number_path
from sources import _cached, _frame, _http_json, is_offline, register_source

BRK_VECS = "https://bitview.space/api/vecs"
BRK_TTL_S = 21_600   # daily series: refresh ~4x a day, serve stale on a blip

# frame id -> (BRK series name, catalogue label). Order = catalogue order.
SERIES = {
    "sats_per_usd":   ("price_close_sats", "Sats per USD"),
    "mvrv":           ("mvrv", "MVRV ratio"),
    "days_since_ath": ("days_since_price_ath", "Days since ATH"),
    "realized_price": ("realized_price", "Realized price (USD)"),
    "puell":          ("puell_multiple", "Puell multiple"),
    "nupl":           ("nupl", "NUPL"),
    "price_ath":      ("price_ath", "All-time high (USD)"),
    "supply":         ("supply", "BTC supply (M)"),
    "market_cap":     ("market_cap", "Market cap ($T)"),
}

SYNTHETIC_BRK = {
    "price_close_sats": 1547.0,
    "mvrv": 1.226,
    "days_since_price_ath": 302.65,
    "realized_price": 52_719.5,
    "puell_multiple": 0.758,
    "nupl": 0.184,
    "price_ath": 125_651.43,
    "supply": 20_068_154.26,
    "market_cap": 1_297_065_415_669.0,
}


def series_value(series):
    """Latest daily value of a BRK series as a float, or None. `?from=-1`
    answers with the last datapoint as a bare number (tolerate a one-element
    list); anything else - including the API's helpful wrong-name error -
    parses to None and the frame skips."""
    if is_offline():
        return SYNTHETIC_BRK.get(series)

    def fetch():
        d = _http_json(f"{BRK_VECS}/dateindex-to-{series}?from=-1")
        if isinstance(d, list) and d:
            d = d[-1]
        try:
            return float(d)
        except (TypeError, ValueError):
            return None
    return _cached(f"brk:{series}", BRK_TTL_S, fetch)


# --------------------------------------------------------------------------- #
# Colours. Analytics wear the calm neutral; the three regime oscillators tint
# by the widely-used chart bands (value zone green, overheated red).
# --------------------------------------------------------------------------- #

def _mvrv_color(v):
    if v < 1:
        return LED_GREEN
    if v > 3:
        return LED_RED
    return LED_WARM_WHITE


def _nupl_color(v):
    if v < 0:
        return LED_GREEN
    if v > 0.75:
        return LED_RED
    return LED_WARM_WHITE


def _puell_color(v):
    if v < 0.5:
        return LED_GREEN
    if v > 4:
        return LED_RED
    return LED_WARM_WHITE


# --------------------------------------------------------------------------- #
# Renderers. Each returns a frame dict or None (a value the 7 slots cannot
# show honestly is skipped, never truncated).
# --------------------------------------------------------------------------- #

def _int_render(tl, br=None):
    def build(fid, v):
        n = int(round(v))
        if len(str(n)) > 7:
            return None
        return _frame(fid, show_number_path(n, tl=tl, br=br),
                      number=n, color=LED_WARM_WHITE)
    return build


def _ratio_render(tl, br=None, color=None):
    def build(fid, v):
        val = f"{v:.2f}"
        if len(val) > 7:
            return None
        return _frame(fid, show_number_path(val, tl=tl, br=br),
                      number=val,
                      color=color(v) if color else LED_WARM_WHITE)
    return build


def _scaled_render(tl, br, div):
    """Compact a big number honestly: scale it and say so in the small
    lines (e.g. supply 20068154 -> 20.07 "M BTC", cap 1.30 "$T")."""
    def build(fid, v):
        val = f"{v / div:.2f}"
        if len(val) > 7:
            return None
        return _frame(fid, show_number_path(val, tl=tl, br=br),
                      number=val, color=LED_WARM_WHITE)
    return build


_RENDERERS = {
    "sats_per_usd":   _int_render("sats per USD"),
    "mvrv":           _ratio_render("MVRV", "ratio", _mvrv_color),
    "days_since_ath": _int_render("days since", "price ATH"),
    "realized_price": _int_render("realized", "price USD"),
    "puell":          _ratio_render("Puell", "multiple", _puell_color),
    "nupl":           _ratio_render("NUPL", "ratio", _nupl_color),
    "price_ath":      _int_render("all-time high", "USD"),
    "supply":         _scaled_render("supply", "M BTC", 1e6),
    "market_cap":     _scaled_render("mkt cap", "$T", 1e12),
}


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #

def _brk_frames(options, wanted=None):
    chosen = {s for s in options.get("series") or [] if s in SERIES}

    def want(fid):
        if wanted is None:
            return True   # library view: the series picker is ignored
        return fid in wanted and fid in chosen

    frames = []
    for fid, (series, _label) in SERIES.items():
        if not want(fid):
            continue
        v = series_value(series)
        if v is None:
            continue  # source down / bad series name: skip, never crash
        try:
            f = _RENDERERS[fid](fid, float(v))
        except (TypeError, ValueError):
            continue
        if f:
            frames.append(f)
    return frames


register_source(
    "brk",
    label="On-chain analytics",
    category="analytics",
    description="Daily on-chain analytics from the Bitcoin Research Kit "
                "(bitview.space, keyless): MVRV, NUPL, Puell, realized "
                "price, days since ATH, supply and market cap. USD-based, "
                "updated once a day.",
    frames=[(fid, label) for fid, (_s, label) in SERIES.items()],
    builder=_brk_frames,
    options_schema={
        "series": {"type": "multi", "label": "Series to show",
                   "choices": [{"id": fid, "label": label}
                               for fid, (_s, label) in SERIES.items()],
                   "default": list(SERIES)},
    },
)
