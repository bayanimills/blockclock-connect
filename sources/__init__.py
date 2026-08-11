"""Data-source registry + shared plumbing.

Each source lives in its own module under sources/ and registers itself here:
frames it can build, the options schema a UI needs to render its settings, and
which option keys are SECRETS (never echoed by any API response, never logged).

Frame dicts keep the proven shape everywhere:
    {"name", "label", "source", "path", "slotargs": {..., "color"}}
`path` is the exact BLOCKCLOCK display URL; `slotargs` feed preview_slots()
(with the LED colour riding along under "color", popped before rendering) -
the same shape a proven predecessor feeder uses, so the ClockClient and
preview logic are shared.

Rules:
  * a source that errors is SKIPPED, never crashes the loop;
  * network reads are cached (per-endpoint TTLs) and stale-tolerant (a blip
    at mempool.space must not blank half the rotation);
  * BC_OFFLINE=1 (or set_offline(True)) swaps EVERY fetcher for synthetic
    data so previews/selfcheck run with zero network;
  * credentialed fetchers reduce every failure to an HTTP status code or an
    exception CLASS NAME - nothing that could carry a secret into a log.
"""

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from clock import SLOTS

log = logging.getLogger("sources")

MEMPOOL_API = "https://mempool.space/api"
COINBASE_API = "https://api.coinbase.com/v2/prices"
GEOCODE_API = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"

HALVING_INTERVAL = 210_000
CACHE_TTL_S = 300           # default TTL for live reads
GEOCODE_TTL_S = 86_400      # a city's coordinates don't move
SHOPIFY_TTL_S = 60          # today's orders change fast, but don't hammer
SHOPIFY_API_VERSION = "2024-10"

CURRENCIES = ["USD", "AUD", "EUR", "GBP", "CAD", "JPY", "CHF", "NZD", "SGD",
              "HKD", "SEK", "NOK", "DKK", "PLN", "CZK", "BRL", "MXN", "ZAR",
              "INR", "AED"]

# the LEGACY network stat picker (kept so existing configs + the current UI
# keep working; every NEW frame is selected via the rotation instead)
NETWORK_STATS = ["block_height", "fees", "halving", "difficulty", "mempool"]

_OFFLINE = os.environ.get("BC_OFFLINE") == "1"


def set_offline(value=True):
    global _OFFLINE
    _OFFLINE = bool(value)


def is_offline():
    return _OFFLINE


# --------------------------------------------------------------------------- #
# HTTP + cache plumbing
# --------------------------------------------------------------------------- #

_CACHE = {}  # key -> (fetched_at, value)


def _http_json(url, timeout=12, headers=None):
    """Keyless GET -> parsed JSON (or raw text for bare-value endpoints).
    Returns None on ANY failure; never raises out of a frame builder.
    Keyless URLs only - anything credentialed goes through _http_redacted."""
    try:
        h = {"User-Agent": "blockclock-connect/1"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace").strip()
        try:
            return json.loads(body)
        except ValueError:
            return body  # e.g. /blocks/tip/height returns a bare integer
    except Exception as e:
        log.info("GET %s failed: %r", url, e)
        return None


def _http_redacted(url, headers=None, data=None, timeout=15, insecure=False):
    """Credentialed fetch -> (parsed json/text, None) or (None, why).
    `why` is a status code or exception CLASS NAME only - nothing that could
    ever carry a token, macaroon or password into a log line or error message.
    The URL itself is never logged either (keys can ride in query strings).
    insecure=True tolerates a self-signed TLS cert (LND's REST API)."""
    h = {"User-Agent": "blockclock-connect/1"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    kwargs = {"timeout": timeout}
    if insecure and url.startswith("https://"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["context"] = ctx
    try:
        with urllib.request.urlopen(req, **kwargs) as r:
            body = r.read().decode("utf-8", "replace").strip()
        try:
            return json.loads(body), None
        except ValueError:
            return body, None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, type(e).__name__


def _cached(key, ttl, fetch):
    """TTL cache that serves STALE data when a refresh fails, rather than
    dropping the frame."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fetch()
    if val is not None:
        _CACHE[key] = (now, val)
        return val
    return hit[1] if hit else None


# --------------------------------------------------------------------------- #
# Shared display helpers
# --------------------------------------------------------------------------- #

# currencies the device can honestly show with a $ symbol; others get the
# currency code spelled out in the small lines instead (sym would mislead)
_DOLLAR_CCYS = {"USD", "AUD", "CAD", "NZD", "SGD", "HKD", "MXN", "BRL"}


def ccy_symbol(ccy):
    return "$" if ccy in _DOLLAR_CCYS else None


def token_hint(token):
    """A safe display hint for a stored secret: first 6 + last 4 characters,
    never the value. Empty string when nothing is stored."""
    t = str(token or "")
    if not t:
        return ""
    if len(t) >= 14:
        return f"{t[:6]}…{t[-4:]}"
    return "saved"


def _fit_line(raw, limit=13):
    """Fit a small-text line (tl/br). The device's small lines hold roughly a
    dozen characters; keep it tidy rather than let the firmware clip."""
    if not raw:
        return None
    s = str(raw).strip()
    return s if len(s) <= limit else s[:limit]


def compact_sats(sats):
    """Fit sats into <=7 digits. Above that, switch to thousands/millions."""
    if sats is None:
        return None, None
    if sats < 10_000_000:
        return str(sats), None            # raw sats
    if sats < 10_000_000_000:
        return str(sats // 1000), "ksat"  # thousands of sats
    return str(sats // 1_000_000), "Msat"


def fit_city(raw):
    """Fit a place name into the 7 big slots: prefer the first word if the
    whole name won't fit."""
    if not raw:
        return None
    city = str(raw).strip()
    if len(city) <= SLOTS:
        return city
    first = city.split()[0]
    return first if len(first) <= SLOTS else first[:SLOTS]


# --------------------------------------------------------------------------- #
# The registry. Source modules call register_source() at import time; the
# public API (build_frames / catalogue / FRAME_DEFS) is derived from it.
# --------------------------------------------------------------------------- #

REGISTRY = {}     # source id -> spec dict (insertion order = catalogue order)
FRAME_DEFS = {}   # frame id -> (source id, human label)

CATEGORIES = [
    {"id": "price",     "label": "Bitcoin price & market"},
    {"id": "network",   "label": "Bitcoin network"},
    {"id": "analytics", "label": "On-chain analytics"},
    {"id": "node",     "label": "Your node"},
    {"id": "merchant", "label": "Your shop"},
    {"id": "weather",  "label": "Weather & sky"},
    {"id": "macro",    "label": "Macro & markets"},
    {"id": "space",    "label": "Space & time"},
    {"id": "novelty",  "label": "Novelty"},
]
_CATEGORY_IDS = {c["id"] for c in CATEGORIES}


def register_source(sid, *, label, category, description, frames, builder,
                    options_schema, advanced=False, secrets=(), validate=None):
    """Called once per source module at import. `frames` is an ordered list of
    (frame_id, human label); frame ids are globally unique. `secrets` names
    option keys that must never leave the box (redacted to *_set / *_hint).
    `validate` is an optional hook(src_out, fail) run after generic schema
    validation on save."""
    if sid in REGISTRY:
        raise ValueError(f"duplicate source id {sid!r}")
    if category not in _CATEGORY_IDS:
        raise ValueError(f"unknown category {category!r} for source {sid!r}")
    for fid, flabel in frames:
        if fid in FRAME_DEFS:
            raise ValueError(f"duplicate frame id {fid!r}")
        FRAME_DEFS[fid] = (sid, flabel)
    REGISTRY[sid] = {
        "id": sid, "label": label, "category": category,
        "description": description, "frames": list(frames),
        "builder": builder, "options_schema": options_schema,
        "advanced": bool(advanced), "secrets": tuple(secrets),
        "validate": validate,
    }


def frame_category(frame_id):
    sid = FRAME_DEFS.get(frame_id, (None,))[0]
    spec = REGISTRY.get(sid)
    return spec["category"] if spec else None


def _frame(name, path, **slotargs):
    src, label = FRAME_DEFS[name]
    return {"name": name, "label": label, "source": src,
            "path": path, "slotargs": slotargs}


# --------------------------------------------------------------------------- #
# Public API: build_frames + catalogue
# --------------------------------------------------------------------------- #

def rotation_ids(config):
    """The rotation as a clean list of known frame ids. Entries may be plain
    strings or {"id": ...} dicts (the dict form also carries dwell/window,
    folded into config["frames"] by validation)."""
    out = []
    for item in config.get("rotation") or []:
        fid = item.get("id") if isinstance(item, dict) else item
        if isinstance(fid, str) and fid in FRAME_DEFS and fid not in out:
            out.append(fid)
    return out


def build_frames(config, all_frames=False):
    """Frames for the enabled sources. Default (the feeder's view): a frame
    appears only if its source is enabled AND its id is in the rotation,
    ordered by the rotation. all_frames=True (the preview/test view): every
    frame an enabled source can build, rotation-ordered first, the rest
    appended - and legacy per-source pickers (network.stats, shopify.frames,
    weather.show_condition) are ignored so the UI can show everything.
    A source that errors contributes nothing; the loop never crashes."""
    sources_cfg = config.get("sources", {})
    rot = rotation_ids(config)
    wanted = None if all_frames else set(rot)
    frames = []
    for sid, spec in REGISTRY.items():
        src = sources_cfg.get(sid) or {}
        if not src.get("enabled"):
            continue
        if wanted is not None \
                and not any(fid in wanted for fid, _ in spec["frames"]):
            continue  # nothing from this source is in rotation: skip cheaply
        try:
            frames.extend(spec["builder"](src.get("options") or {}, wanted))
        except Exception as e:  # belt and braces: a source bug skips, not kills
            log.warning("source %s failed: %r", sid, e)
    by_name = {f["name"]: f for f in frames}
    ordered = [by_name.pop(n) for n in rot if n in by_name]
    if all_frames:
        ordered.extend(f for f in frames if f["name"] in by_name)
    return ordered


def public_options(spec, saved):
    """A source's options as any API response may see them: schema keys (with
    defaults filled in), any extra persisted keys (e.g. weather lat/lon), and
    every secret REPLACED by <key>_set / <key>_hint. The raw secret value
    never leaves the box."""
    saved = saved or {}
    secrets = set(spec["secrets"])
    out = {}
    for key, meta in spec["options_schema"].items():
        if key in secrets:
            continue
        out[key] = saved.get(key, meta.get("default"))
    for key, val in saved.items():
        if key in secrets or key.startswith("clear_") or key in out:
            continue
        out[key] = val
    for key in secrets:
        val = saved.get(key, "") or ""
        out[f"{key}_set"] = bool(val)
        out[f"{key}_hint"] = token_hint(val)
    return out


def catalogue(config):
    """The /api/sources payload: every registered source with its category,
    options schema, currently-saved (redacted) options, and its frames -
    each with a stable id, human label and category - enough for a UI to
    list every frame grouped by category and toggle it."""
    sources_cfg = config.get("sources", {})
    out = []
    for sid, spec in REGISTRY.items():
        src = sources_cfg.get(sid) or {}
        entry = {
            "id": sid,
            "label": spec["label"],
            "category": spec["category"],
            "description": spec["description"],
            "enabled": bool(src.get("enabled")),
            "options_schema": spec["options_schema"],
            "options": public_options(spec, src.get("options") or {}),
            "frames": [{"id": fid, "label": flabel,
                        "category": spec["category"]}
                       for fid, flabel in spec["frames"]],
        }
        if spec["advanced"]:
            entry["advanced"] = True
        out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# Source modules register themselves on import (order = catalogue order).
# --------------------------------------------------------------------------- #

from sources import price as _price_mod          # noqa: E402,F401
from sources import network as _network_mod      # noqa: E402,F401
from sources import brk as _brk_mod              # noqa: E402,F401
from sources import node as _node_mod            # noqa: E402,F401
from sources import weather as _weather_mod      # noqa: E402,F401
from sources import macro as _macro_mod          # noqa: E402,F401
from sources import space as _space_mod          # noqa: E402,F401
from sources import novelty as _novelty_mod      # noqa: E402,F401
from sources import merchant as _merchant_mod    # noqa: E402,F401

# Re-export the API the rest of the app (and its tests) already use, so
# `sources.X` keeps meaning what it always did - including monkeypatching
# sources.shopify_snapshot / sources.shopify_validate in the selfcheck.
from sources.price import btc_price, sats_per_unit           # noqa: E402,F401
from sources.network import fee_color, network_snapshot      # noqa: E402,F401
from sources.weather import (geocode, geocode_search,        # noqa: E402,F401
                             temp_color, weather_current, wmo_word)
from sources.merchant import (DEFAULT_SHOPIFY_FRAMES,        # noqa: E402,F401
                              SHOPIFY_FRAME_LABELS, SHOPIFY_FRAMES,
                              build_milestone, build_sale_alert, goal_color,
                              shopify_day_key, shopify_snapshot,
                              shopify_validate)
