#!/usr/bin/env python3
"""BlockClock Connect - point a Coinkite BLOCKCLOCK (mini/micro) at the data
you care about, from one friendly web page.

Runs two things in one container:
  * this Flask app (config UI + JSON API) on 0.0.0.0:4200
  * a background Feeder thread that drives the connected clock 24/7 with the
    enabled feeds, using the rate/stall/LED discipline proven in the field
    (see clock.py / feeder.py). On SIGTERM it hands the clock back to its own
    screens, fast and best-effort.

Persistence lives under $DATA_DIR (default /data): config.json + state.json.

    python app.py                 run the app (web + feeder)
    python app.py --selfcheck     offline smoke test, prints PASS/FAIL
"""

import copy
import functools
import hmac
import logging
import os
import re
import secrets
import signal
import sys

from flask import Flask, jsonify, request

import discovery
import sources
from clock import SLOTS, preview_slots, show_number_path, show_text_path
from feeder import Feeder
from sources import CATEGORIES, CURRENCIES, DEFAULT_SHOPIFY_FRAMES, \
    FRAME_DEFS, NETWORK_STATS, SHOPIFY_FRAMES, build_frames, catalogue, \
    frame_category, geocode, geocode_search, token_hint
from sources.price import EXCHANGES
from store import MIN_WRITE_INTERVAL_S, Store

log = logging.getLogger("app")

PORT = int(os.environ.get("PORT", "4200"))


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #

class ConfigError(ValueError):
    pass


def _merge_secret(opts_in, cur_opts, key):
    """Secret semantics shared by EVERY credentialed option (Shopify token,
    Bitaroo key, Core password, LND macaroon, BTCPay/FRED keys): a non-empty
    incoming value SETS it, an explicit clear_<key> flag removes it, and a
    blank/absent field KEEPS the saved one (a normal save never clobbers a
    credential - the UI only ever sees <key>_set / <key>_hint)."""
    if bool(opts_in.get(f"clear_{key}")):
        return ""
    incoming = opts_in.get(key)
    if isinstance(incoming, str) and incoming.strip():
        return incoming.strip()
    return str(cur_opts.get(key) or "")


def _validate_generic(sid, spec, s_in, cur_opts, fail):
    """Schema-driven validation for a registry source: coerce each option by
    its declared type, apply the shared secret semantics, then run the
    source's own validate hook (if any). Returns {"enabled", "options"}."""
    opts_in = s_in.get("options") or {}
    out_opts = {}
    for key, meta in spec["options_schema"].items():
        if key in spec["secrets"]:
            out_opts[key] = _merge_secret(opts_in, cur_opts, key)
            continue
        raw = opts_in.get(key, cur_opts.get(key, meta.get("default")))
        kind = meta.get("type")
        if kind == "select":
            ids = [c["id"] if isinstance(c, dict) else c
                   for c in meta.get("choices") or []]
            if raw not in ids:
                if raw not in (None, ""):
                    fail(f"{sid}.{key}: unsupported value")
                raw = meta.get("default")
            out_opts[key] = raw
        elif kind == "multi":
            ids = {c["id"] if isinstance(c, dict) else c
                   for c in meta.get("choices") or []}
            if raw is not None and not isinstance(raw, list):
                fail(f"{sid}.{key} must be a list")
                raw = []
            out_opts[key] = [v for v in (raw or []) if v in ids]
        elif kind == "number":
            try:
                num = float(raw if raw is not None
                            else meta.get("default") or 0)
            except (TypeError, ValueError):
                fail(f"{sid}.{key} must be a number")
                num = float(meta.get("default") or 0)
            lo, hi = meta.get("min"), meta.get("max")
            if (lo is not None and num < lo) or (hi is not None and num > hi):
                fail(f"{sid}.{key} out of range")
                num = float(meta.get("default") or 0)
            out_opts[key] = int(num) if float(num).is_integer() else num
        elif kind == "bool":
            out_opts[key] = bool(raw)
        else:  # text
            out_opts[key] = str(raw or "").strip()
    src_out = {"enabled": bool(s_in.get("enabled")), "options": out_opts}
    if spec.get("validate"):
        spec["validate"](src_out, fail)
    return src_out


def _validate_sources(payload_sources, current_sources, strict=True):
    """Validate + normalise the sources block. Returns (sources, warnings).
    strict=True raises ConfigError on bad input (the /api/config contract);
    strict=False silently falls back (the live-preview path)."""
    warnings = []
    out = {}

    def fail(msg):
        if strict:
            raise ConfigError(msg)
        warnings.append(msg)

    cur = current_sources or {}
    src = payload_sources or {}

    # -- price (a provider: exchange + currency + optional Bitaroo token)
    p = src.get("price", cur.get("price", {})) or {}
    popt = p.get("options") or {}
    cur_popt = (cur.get("price") or {}).get("options") or {}
    exchange = str(popt.get("exchange",
                            cur_popt.get("exchange") or "coinbase")
                   or "coinbase").lower()
    if exchange not in EXCHANGES:
        fail(f"Unknown exchange '{exchange}'")
        exchange = "coinbase"
    ccy = (popt.get("currency") or "USD").upper()
    if ccy not in CURRENCIES:
        fail(f"Unsupported currency '{ccy}'")
        ccy = "USD"
    # DEPRECATED: Bitaroo went keyless, so no exchange requires a key any
    # more. A bitaroo_api_key saved by an older version still loads (and
    # stays redacted everywhere) - it is just never read.
    bitaroo_key = _merge_secret(popt, cur_popt, "bitaroo_api_key")
    out["price"] = {"enabled": bool(p.get("enabled")),
                    "options": {"exchange": exchange, "currency": ccy,
                                "bitaroo_api_key": bitaroo_key}}

    # -- network
    n = src.get("network", cur.get("network", {})) or {}
    stats_in = (n.get("options") or {}).get("stats", [])
    if not isinstance(stats_in, list):
        fail("network.options.stats must be a list")
        stats_in = []
    stats, bad = [], []
    for s in stats_in:
        (stats if s in NETWORK_STATS else bad).append(s)
    if bad:
        fail(f"Unknown network stats: {', '.join(map(str, bad))}")
    out["network"] = {"enabled": bool(n.get("enabled")),
                      "options": {"stats": stats}}

    # -- weather
    w = src.get("weather", cur.get("weather", {})) or {}
    wopt = w.get("options") or {}
    city = str(wopt.get("city") or "").strip()
    units = "F" if str(wopt.get("units", "C")).upper() == "F" else "C"
    show_cond = bool(wopt.get("show_condition"))
    enabled = bool(w.get("enabled"))

    def _coord(val, lo, hi):
        try:
            num = float(val)
        except (TypeError, ValueError):
            return None
        return num if lo <= num <= hi else None

    # a typeahead pick rides along as explicit lat/lon (+ place), so the
    # saved config resolves to exactly that location - no ambiguity at render
    in_lat = _coord(wopt.get("lat"), -90, 90)
    in_lon = _coord(wopt.get("lon"), -180, 180)
    in_place = str(wopt.get("place") or "").strip()
    lat = lon = place = None
    if enabled:
        if not city:
            fail("Enter a city for the weather feed")
            enabled = False
        elif in_lat is not None and in_lon is not None:
            lat, lon = in_lat, in_lon
            place = in_place or city
        else:
            cur_wopt = (cur.get("weather") or {}).get("options") or {}
            if (city.lower() == str(cur_wopt.get("city") or "").lower()
                    and cur_wopt.get("lat") is not None):
                lat, lon = cur_wopt["lat"], cur_wopt["lon"]
                place = cur_wopt.get("place") or city
            else:
                geo = geocode(city)
                if not geo:
                    fail(f"Couldn't find a place called '{city}' - "
                         "try adding a country, e.g. 'Springfield, US'")
                    enabled = False
                else:
                    lat, lon = geo["lat"], geo["lon"]
                    place = geo["name"]
    out["weather"] = {"enabled": enabled,
                      "options": {"city": city, "units": units,
                                  "show_condition": show_cond,
                                  "lat": lat, "lon": lon, "place": place}}

    # -- shopify (advanced / merchant). The Admin token lives ONLY in the
    #    saved config on the user's own disk; every API response strips it
    #    (public_config() below + sources.catalogue()).
    sh = src.get("shopify", cur.get("shopify", {})) or {}
    sopt = sh.get("options") or {}
    cur_sopt = (cur.get("shopify") or {}).get("options") or {}
    existing_token = str(cur_sopt.get("token") or "")

    domain = str(sopt.get("shop_domain",
                          cur_sopt.get("shop_domain") or "") or "")
    domain = domain.strip().lower()
    for junk in ("https://", "http://"):
        if domain.startswith(junk):
            domain = domain[len(junk):]
    domain = domain.strip("/")
    if domain and (" " in domain or "." not in domain):
        fail(f"'{domain}' doesn't look like a shop domain")
        domain = ""

    # token: SET when a non-empty value arrives; explicit clear_token removes
    # it; absent/blank KEEPS the saved token (a normal save never clobbers it)
    incoming = sopt.get("token")
    if bool(sopt.get("clear_token")):
        token = ""
    elif isinstance(incoming, str) and incoming.strip():
        token = incoming.strip()
    else:
        token = existing_token

    sccy = str(sopt.get("currency",
                        cur_sopt.get("currency") or "AUD") or "AUD").upper()
    if sccy not in CURRENCIES:
        fail(f"Unsupported currency '{sccy}'")
        sccy = "AUD"

    try:
        goal = max(0.0, float(sopt.get(
            "daily_goal", cur_sopt.get("daily_goal") or 0) or 0))
    except (TypeError, ValueError):
        fail("daily_goal must be a number")
        goal = 0.0

    try:
        tzoff = float(sopt.get("tz_offset_hours",
                               cur_sopt.get("tz_offset_hours", 10)))
    except (TypeError, ValueError):
        fail("tz_offset_hours must be a number")
        tzoff = 10.0
    if not -12 <= tzoff <= 14:
        fail("tz_offset_hours must be between -12 and 14")
        tzoff = 10.0

    frames_in = sopt.get("frames", cur_sopt.get("frames"))
    if frames_in is None:
        frames_in = list(DEFAULT_SHOPIFY_FRAMES)
    if not isinstance(frames_in, list):
        fail("shopify.options.frames must be a list")
        frames_in = []
    sframes, sbad = [], []
    for f in frames_in:
        (sframes if f in SHOPIFY_FRAMES else sbad).append(f)
    if sbad:
        fail(f"Unknown Shopify frames: {', '.join(map(str, sbad))}")

    flash = bool(sopt.get("flash_on_sale",
                          cur_sopt.get("flash_on_sale", True)))
    sh_enabled = bool(sh.get("enabled"))
    new_token = bool(token) and token != existing_token

    if (sh_enabled or new_token) and domain \
            and not domain.endswith(".myshopify.com"):
        fail("Use your .myshopify.com domain (that's where the Admin API "
             "lives), e.g. myshop.myshopify.com")
    if new_token and not domain:
        fail("Add your shop domain so the token can be verified")
    if sh_enabled and (not domain or not token):
        fail("Shopify needs your shop domain and an Admin API access token")
        sh_enabled = False
    if strict and new_token and domain:
        # prove the pair against the shop before storing/enabling anything
        ok, msg = sources.shopify_validate(domain, token)
        if not ok:
            raise ConfigError(f"Shopify: {msg}")

    out["shopify"] = {"enabled": sh_enabled,
                      "options": {"shop_domain": domain, "token": token,
                                  "currency": sccy, "daily_goal": goal,
                                  "tz_offset_hours": tzoff,
                                  "frames": sframes,
                                  "flash_on_sale": flash}}

    # -- everything else in the registry (node, macro, space, novelty,
    #    btcpay, ...): generic schema-driven validation + per-source hooks.
    #    Secrets get the same keep/clear semantics as the Shopify token.
    for sid, spec in sources.REGISTRY.items():
        if sid in out:
            continue
        s_in = src.get(sid, cur.get(sid, {})) or {}
        out[sid] = _validate_generic(
            sid, spec, s_in, (cur.get(sid) or {}).get("options") or {}, fail)
    return out, warnings


def _clean_frame_setting(fid, raw):
    """Per-frame settings: {"dwell": N, "window": {from_hour, to_hour}} ->
    normalised dict ({} when everything is at its default; defaults are not
    stored). Raises ConfigError on malformed input."""
    if not isinstance(raw, dict):
        raise ConfigError(f"frame setting for '{fid}' must be an object")
    out = {}
    if raw.get("dwell") is not None:
        try:
            dwell = int(raw["dwell"])
        except (TypeError, ValueError):
            raise ConfigError(f"{fid}.dwell must be a whole number")
        if not 1 <= dwell <= 48:
            raise ConfigError(f"{fid}.dwell must be between 1 and 48 "
                              "write-windows")
        if dwell > 1:
            out["dwell"] = dwell
    win = raw.get("window")
    if win is not None:
        if not isinstance(win, dict):
            raise ConfigError(f"{fid}.window must be an object with "
                              "from_hour and to_hour")
        try:
            f_h, t_h = int(win["from_hour"]), int(win["to_hour"])
        except (TypeError, KeyError, ValueError):
            raise ConfigError(f"{fid}.window needs whole-number from_hour "
                              "and to_hour")
        if not (0 <= f_h <= 23 and 0 <= t_h <= 23):
            raise ConfigError(f"{fid}.window hours must be between 0 and 23")
        if f_h != t_h:  # equal bounds mean 'always' - don't store a no-op
            out["window"] = {"from_hour": f_h, "to_hour": t_h}
    return out


def validate_config(payload, current, strict=True):
    """Payload {sources, rotation, frames, write_interval_s} -> full merged
    config. Raises ConfigError (strict) or degrades with warnings (lenient).
    `rotation` is the ordered list of frame ids the user picked (plain ids,
    or {"id", "dwell", "window"} objects whose settings fold into `frames`);
    `frames` is the per-frame settings map {frame_id: {dwell, window}}."""
    if not isinstance(payload, dict):
        raise ConfigError("Expected a JSON object")
    cfg = dict(current)
    warnings = []

    if "write_interval_s" in payload:
        try:
            interval = int(payload["write_interval_s"])
        except (TypeError, ValueError):
            raise ConfigError("write_interval_s must be a number")
        if interval < MIN_WRITE_INTERVAL_S:
            msg = (f"write_interval_s must be at least {MIN_WRITE_INTERVAL_S} "
                   "- the clock rate-limits display writes to about one per "
                   "minute (HTTP 429 above that)")
            if strict:
                raise ConfigError(msg)
            warnings.append(msg)
            interval = MIN_WRITE_INTERVAL_S
        cfg["write_interval_s"] = interval

    srcs, w = _validate_sources(payload.get("sources"), current.get("sources"),
                                strict=strict)
    warnings.extend(w)
    cfg["sources"] = srcs

    if "frames" in payload:
        fs_in = payload["frames"]
        if not isinstance(fs_in, dict):
            raise ConfigError("frames must be an object of per-frame "
                              "settings")
        fsettings = {}
        for fid, raw in fs_in.items():
            if fid not in FRAME_DEFS:
                continue  # unknown ids dropped silently, like rotation
            setting = _clean_frame_setting(fid, raw)
            if setting:
                fsettings[fid] = setting
        cfg["frames"] = fsettings

    if "rotation" in payload:
        rot_in = payload["rotation"]
        if not isinstance(rot_in, list):
            raise ConfigError("rotation must be a list of frame ids")
        seen, rotation = set(), []
        fsettings = dict(cfg.get("frames") or {})
        folded = False
        for item in rot_in:
            fid, extra = item, None
            if isinstance(item, dict):  # {"id", "dwell", "window"} form
                fid, extra = item.get("id"), item
            if not isinstance(fid, str) or fid not in FRAME_DEFS \
                    or fid in seen:
                continue
            rotation.append(fid)
            seen.add(fid)
            if extra is not None:
                folded = True
                setting = _clean_frame_setting(
                    fid, {k: v for k, v in extra.items() if k != "id"})
                if setting:
                    fsettings[fid] = setting
                else:
                    fsettings.pop(fid, None)
        cfg["rotation"] = rotation
        if folded:
            cfg["frames"] = fsettings
    return cfg, warnings


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def public_config(cfg):
    """The config as any API response is allowed to see it: EVERY secret in
    the registry (Shopify token, Bitaroo key, Core RPC password, LND
    macaroon, BTCPay key, FRED key) is REPLACED by {<key>_set, <key>_hint}.
    Every endpoint that echoes config goes through here - no raw credential
    ever leaves the box."""
    out = {k: copy.deepcopy(cfg.get(k)) for k in
           ("sources", "rotation", "frames", "write_interval_s")}
    for sid, spec in sources.REGISTRY.items():
        s = (out.get("sources") or {}).get(sid)
        if not isinstance(s, dict) or not spec["secrets"]:
            continue
        opts = s.get("options") or {}
        for key in spec["secrets"]:
            val = opts.pop(key, "") or ""
            opts.pop(f"clear_{key}", None)
            opts[f"{key}_set"] = bool(val)
            opts[f"{key}_hint"] = token_hint(val)
        s["options"] = opts
    # agent/API access: the bearer token is a secret like every other - the
    # echo only ever carries enabled + set/hint. The ONE deliberate reveal is
    # GET /api/access-token (the owner copying it for their assistant).
    acc = cfg.get("api_access") or {}
    out["api_access"] = {"enabled": bool(acc.get("enabled")),
                         "token_set": bool(acc.get("token")),
                         "token_hint": token_hint(acc.get("token"))}
    return out


def new_api_token():
    """A fresh agent bearer token: a recognisable prefix + 32 bytes of
    CSPRNG entropy (urlsafe, ~43 chars)."""
    return "bcc_" + secrets.token_urlsafe(32)


def openapi_spec():
    """OpenAPI 3.1 description of the /agent/* endpoints ONLY, written for
    an LLM tool-caller (OpenAI function-calling, Claude tools, or anything
    that imports OpenAPI). The UI/config endpoints are deliberately absent -
    an agent gets display control, nothing else."""
    err = {"type": "object",
           "properties": {"ok": {"type": "boolean", "const": False},
                          "error": {"type": "string"}}}
    frame_id = {"type": "string",
                "description": "A frame id from GET /agent/frames, e.g. "
                               "'btc_price' or 'moscow_time'."}
    sec = [{"bearerAuth": []}]
    resp_errors = {
        "400": {"description": "Invalid input - the error message says "
                               "exactly what to fix.",
                "content": {"application/json": {"schema": err}}},
        "401": {"description": "Missing or wrong bearer token.",
                "content": {"application/json": {"schema": err}}},
        "403": {"description": "API access is switched off in the app.",
                "content": {"application/json": {"schema": err}}},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "BlockClock Connect - agent control API",
            "version": "1",
            "description":
                "Drive a Coinkite BLOCKCLOCK (mini/micro) through the "
                "BlockClock Connect app. The clock is a 7-character e-paper "
                "display that accepts about ONE change per 65 seconds; every "
                "push here is queued and shown at the next write window "
                "(the response's eta_s/note say when). Requires the owner "
                "to have enabled API access in the app and to have shared "
                "the bearer token.",
        },
        "servers": [{
            "url": "/",
            "description": "Relative to wherever the app is served. When "
                           "importing this schema into an external tool, "
                           "replace with the app's base URL, e.g. "
                           "http://umbrel.local:4200",
        }],
        "security": sec,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http", "scheme": "bearer",
                    "description": "The token from the app's Advanced > API "
                                   "access settings (starts with 'bcc_').",
                }
            }
        },
        "paths": {
            "/agent/state": {"get": {
                "operationId": "get_state",
                "summary": "Current clock state",
                "description": "Whether a clock is connected and being "
                               "driven, the frame currently on its display, "
                               "seconds until the next write window, and "
                               "the rotation of frame ids.",
                "security": sec,
                "responses": {"200": {
                    "description": "Current state.",
                    "content": {"application/json": {"schema": {
                        "type": "object", "properties": {
                            "ok": {"type": "boolean"},
                            "connected": {"type": "boolean"},
                            "clock": {"type": ["object", "null"]},
                            "running": {"type": "boolean"},
                            "last_frame": {"type": ["object", "null"]},
                            "next_write_in_s": {"type": ["number", "null"]},
                            "write_interval_s": {"type": "number"},
                            "rotation": {"type": "array",
                                         "items": {"type": "string"}},
                        }}}}},
                    **{k: v for k, v in resp_errors.items() if k != "400"}},
            }},
            "/agent/frames": {"get": {
                "operationId": "list_frames",
                "summary": "List every frame the app can show",
                "description": "The library of ready-made stats (Bitcoin "
                               "price, block height, fees, weather, ...). "
                               "Each has a stable id to use with "
                               "POST /agent/frame or /agent/rotation; "
                               "'available' means its data source is "
                               "enabled and can render right now.",
                "security": sec,
                "responses": {"200": {
                    "description": "The frame library.",
                    "content": {"application/json": {"schema": {
                        "type": "object", "properties": {
                            "ok": {"type": "boolean"},
                            "frames": {"type": "array", "items": {
                                "type": "object", "properties": {
                                    "id": {"type": "string"},
                                    "label": {"type": "string"},
                                    "category": {"type": "string"},
                                    "available": {"type": "boolean"},
                                    "in_rotation": {"type": "boolean"},
                                }}}}}}}},
                    **{k: v for k, v in resp_errors.items() if k != "400"}},
            }},
            "/agent/show": {"post": {
                "operationId": "show",
                "summary": "Put custom text or a number on the clock now",
                "description": "Queues a one-off frame that preempts the "
                               "rotation for one write window (~65s), then "
                               "the rotation resumes. The display has 7 "
                               "character slots; 'pair' (e.g. BTC/USD) uses "
                               "the first slot, so only 6 remain for "
                               "digits. tl/br are small caption lines "
                               "(about 13 characters) above/below.",
                "security": sec,
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object",
                        "description": "Send exactly one of text/number.",
                        "properties": {
                            "text": {"type": "string", "maxLength": 7,
                                     "description": "Up to 7 characters, "
                                                    "shown uppercase."},
                            "number": {"type": ["number", "string"],
                                       "description": "Up to 7 digits "
                                                      "(6 with a pair)."},
                            "sym": {"type": "string", "maxLength": 1,
                                    "description": "Optional currency "
                                                   "symbol before a "
                                                   "number, e.g. $."},
                            "pair": {"type": "string",
                                     "description": "Optional X/Y unit in "
                                                    "the first slot, e.g. "
                                                    "BTC/USD or SAT/VB "
                                                    "(numbers only)."},
                            "tl": {"type": "string", "maxLength": 13,
                                   "description": "Small top caption."},
                            "br": {"type": "string", "maxLength": 13,
                                   "description": "Small bottom caption."},
                        }}}}},
                "responses": {"200": {
                    "description": "Queued; eta_s says when it appears.",
                    "content": {"application/json": {"schema": {
                        "type": "object", "properties": {
                            "ok": {"type": "boolean"},
                            "queued": {"type": "object"},
                            "eta_s": {"type": "number"},
                            "note": {"type": "string"},
                        }}}}},
                    "409": {"description": "No clock is connected yet.",
                            "content": {"application/json": {"schema": err}}},
                    **resp_errors},
            }},
            "/agent/frame": {"post": {
                "operationId": "show_frame",
                "summary": "Show a ready-made frame from the library now",
                "description": "Queues one library frame (by id from "
                               "GET /agent/frames) to preempt the rotation "
                               "for one write window, then the rotation "
                               "resumes.",
                "security": sec,
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["frame_id"],
                        "properties": {"frame_id": frame_id}}}}},
                "responses": {"200": {
                    "description": "Queued.",
                    "content": {"application/json": {"schema": {
                        "type": "object", "properties": {
                            "ok": {"type": "boolean"},
                            "frame_id": {"type": "string"},
                            "note": {"type": "string"},
                        }}}}},
                    "409": {"description": "No clock is connected yet.",
                            "content": {"application/json": {"schema": err}}},
                    **resp_errors},
            }},
            "/agent/rotation": {"post": {
                "operationId": "set_rotation",
                "summary": "Replace the clock's rotation",
                "description": "Sets the ordered list of frame ids the "
                               "clock cycles through (one frame per ~65s "
                               "write window). Persists and hot-reloads "
                               "immediately. Ids come from "
                               "GET /agent/frames.",
                "security": sec,
                "requestBody": {"required": True, "content": {
                    "application/json": {"schema": {
                        "type": "object", "required": ["frames"],
                        "properties": {"frames": {
                            "type": "array", "minItems": 1,
                            "items": frame_id}}}}}},
                "responses": {"200": {
                    "description": "Saved; 'inactive' lists any ids whose "
                                   "source is currently disabled.",
                    "content": {"application/json": {"schema": {
                        "type": "object", "properties": {
                            "ok": {"type": "boolean"},
                            "rotation": {"type": "array",
                                         "items": {"type": "string"}},
                            "inactive": {"type": "array",
                                         "items": {"type": "string"}},
                            "note": {"type": "string"},
                        }}}}},
                    **resp_errors},
            }},
        },
    }


def frames_to_preview(frames):
    out = []
    for f in frames:
        slotargs = dict(f["slotargs"])
        color = slotargs.pop("color", None)
        out.append({"name": f["name"], "label": f["label"],
                    "source": f["source"],
                    "category": frame_category(f["name"]),
                    "slots": preview_slots(**slotargs),
                    "color": color, "path": f["path"]})
    return out


def create_app(store, feeder=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")

    def jerr(message, code=400):
        return jsonify({"ok": False, "error": message}), code

    @app.errorhandler(Exception)
    def _unhandled(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify({"ok": False, "error": e.description}), e.code
        log.exception("unhandled error")
        return jsonify({"ok": False,
                        "error": "Something went wrong on the server - "
                                 "check the app logs"}), 500

    # ------------------------------------------------------------- pages #

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True}), 200

    # --------------------------------------------------------------- api #

    @app.get("/api/state")
    def api_state():
        cfg = store.config
        state = store.state
        last_frame = (feeder.last_frame if feeder else None) \
            or state.get("last_frame")
        return jsonify({
            "connected": cfg.get("clock"),
            "running": bool(feeder and feeder.driving),
            "config": public_config(cfg),  # never includes the Shopify token
            "last_frame": last_frame,
            "next_write_in_s": feeder.next_write_in_s() if feeder else None,
            "suggested_subnet": discovery.suggested_subnet(),
            "offline": sources.is_offline(),
        })

    @app.post("/api/discover")
    def api_discover():
        body = request.get_json(silent=True) or {}
        subnet = body.get("subnet", "")
        try:
            clocks = discovery.scan(subnet)
        except ValueError as e:
            return jerr(str(e))
        return jsonify({"ok": True, "clocks": clocks})

    @app.post("/api/connect")
    def api_connect():
        body = request.get_json(silent=True) or {}
        ip = str(body.get("ip") or "").strip()
        if not ip or "/" in ip or " " in ip:
            return jerr("Enter the clock's IP address, e.g. 192.168.1.50")
        clockinfo = discovery.probe(ip, timeout=5)
        if not clockinfo:
            return jerr(f"No BLOCKCLOCK answered at {ip}. Check the IP and "
                        "that the clock is on the same network. (If you just "
                        "pushed something to it, it repaints for 30-60s and "
                        "won't answer - try again in a minute.)", 502)
        cfg = store.config
        cfg["clock"] = clockinfo
        store.save_config(cfg)
        if feeder:
            feeder.notify_config_changed()
        return jsonify({"ok": True, "clock": clockinfo})

    @app.post("/api/disconnect")
    def api_disconnect():
        cfg = store.config
        cfg["clock"] = None
        store.save_config(cfg)
        if feeder:
            feeder.notify_config_changed()
        return jsonify({"ok": True,
                        "note": "The clock will resume its own screens within "
                                "a few minutes."})

    @app.get("/api/geocode")
    def api_geocode():
        """Typeahead lookup for the weather city field. Keyless Open-Meteo
        geocoding, trimmed to what the dropdown needs; any upstream trouble
        (or a blank query) is just an empty list - never an error."""
        return jsonify({"ok": True,
                        "results": geocode_search(request.args.get("q", ""))})

    @app.get("/api/sources")
    def api_sources():
        return jsonify({"ok": True, "sources": catalogue(store.config),
                        "categories": CATEGORIES,
                        "currencies": CURRENCIES})

    @app.post("/api/config")
    def api_config():
        body = request.get_json(silent=True)
        if body is None:
            return jerr("Send a JSON body")
        try:
            cfg, _ = validate_config(body, store.config, strict=True)
        except ConfigError as e:
            return jerr(str(e))
        store.save_config(cfg)
        if feeder:
            feeder.notify_config_changed()
        return jsonify({"ok": True,
                        "config": public_config(cfg)})

    @app.get("/api/preview")
    def api_preview():
        """EVERY frame the currently-enabled sources can build (not just the
        rotation), rendered offline-safe - the library view a picker UI needs.
        Rotation frames come first, in rotation order."""
        frames = build_frames(store.config, all_frames=True)
        return jsonify({"ok": True, "frames": frames_to_preview(frames)})

    @app.post("/api/preview")
    def api_preview_candidate():
        """Live preview of UNSAVED edits: the frames the clock would actually
        rotate (source enabled AND in rotation), rendered from the candidate
        config in the body (never persisted)."""
        body = request.get_json(silent=True) or {}
        candidate = body.get("config") or body
        cfg, warnings = validate_config(candidate, store.config, strict=False)
        frames = build_frames(cfg)
        return jsonify({"ok": True, "frames": frames_to_preview(frames),
                        "warnings": warnings})

    @app.post("/api/test")
    def api_test():
        if not feeder:
            return jerr("Feeder not running", 503)
        body = request.get_json(silent=True) or {}
        ok, msg = feeder.request_test(body.get("frame"))
        if not ok:
            return jerr(msg, 409)
        return jsonify({"ok": True, "note": msg})

    # ------------------------------------------- agent / API access ----- #
    # OPTIONAL and OFF BY DEFAULT. The /api/access* endpoints are part of
    # the normal config UI (they sit behind Umbrel's app-proxy auth like
    # every other /api route); the /agent/* endpoints are for the user's OWN
    # AI assistant and require `Authorization: Bearer <token>` instead of the
    # UI session - and answer NOTHING while api_access.enabled is false.

    @app.get("/api/access-token")
    def api_access_token():
        """The ONE deliberate reveal: the box's owner copying the bearer
        token to hand to their assistant. Reached only through the UI path
        (Umbrel app-proxy auth) - never linked from any /agent response."""
        acc = store.config.get("api_access") or {}
        return jsonify({"ok": True, "enabled": bool(acc.get("enabled")),
                        "token": str(acc.get("token") or "")})

    @app.post("/api/access")
    def api_access_update():
        """Toggle agent API access and/or regenerate the token.
        Body: {enabled?: bool, regenerate?: bool}. First enable generates a
        token; disabling KEEPS it (re-enabling is stable); regenerate
        replaces it (old token stops working immediately)."""
        body = request.get_json(silent=True) or {}
        cfg = store.config
        acc = dict(cfg.get("api_access") or {})
        if bool(body.get("regenerate")):
            acc["token"] = new_api_token()
        if "enabled" in body:
            acc["enabled"] = bool(body["enabled"])
        if acc.get("enabled") and not acc.get("token"):
            acc["token"] = new_api_token()
        cfg["api_access"] = {"enabled": bool(acc.get("enabled")),
                             "token": str(acc.get("token") or "")}
        store.save_config(cfg)
        return jsonify({"ok": True,
                        "api_access": public_config(cfg)["api_access"]})

    def require_agent(fn):
        """Bearer auth for /agent/*: 403 while access is disabled, 401 on a
        missing/wrong token (constant-time compare). No cookies, no session -
        an agent only ever holds the token."""
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            acc = store.config.get("api_access") or {}
            if not acc.get("enabled"):
                return jerr("API access is disabled. The clock's owner can "
                            "turn it on under Advanced in the BlockClock "
                            "Connect app.", 403)
            token = str(acc.get("token") or "")
            header = request.headers.get("Authorization") or ""
            supplied = header[7:].strip() \
                if header.startswith("Bearer ") else ""
            if not token or not supplied or not hmac.compare_digest(
                    supplied.encode(), token.encode()):
                return jerr("Missing or wrong token. Send 'Authorization: "
                            "Bearer <token>' - the owner can copy the token "
                            "from the app's Advanced settings.", 401)
            return fn(*a, **kw)
        return wrapper

    @app.get("/agent/state")
    @require_agent
    def agent_state():
        cfg = store.config
        clock_cfg = cfg.get("clock") or None
        last_frame = (feeder.last_frame if feeder else None) \
            or store.state.get("last_frame")
        return jsonify({
            "ok": True,
            "connected": bool(clock_cfg),
            "clock": ({"model": clock_cfg.get("model"),
                       "ip": clock_cfg.get("ip")} if clock_cfg else None),
            "running": bool(feeder and feeder.driving),
            "last_frame": last_frame,
            "next_write_in_s": feeder.next_write_in_s() if feeder else None,
            "write_interval_s": cfg.get("write_interval_s"),
            "rotation": sources.rotation_ids(cfg),
        })

    @app.get("/agent/frames")
    @require_agent
    def agent_frames():
        cfg = store.config
        try:
            available = {f["name"]
                         for f in build_frames(cfg, all_frames=True)}
        except Exception:
            available = set()
        rot = set(sources.rotation_ids(cfg))
        frames = [{"id": fid, "label": label,
                   "category": frame_category(fid),
                   "available": fid in available,
                   "in_rotation": fid in rot}
                  for fid, (_sid, label) in FRAME_DEFS.items()]
        return jsonify({
            "ok": True, "frames": frames,
            "note": "'available' frames can be shown right now (their data "
                    "source is enabled and answering). Use an id with "
                    "POST /agent/frame or POST /agent/rotation."})

    _PAIR_RE = re.compile(r"^[A-Z0-9.]{1,4}/[A-Z0-9.]{1,4}$")

    @app.post("/agent/show")
    @require_agent
    def agent_show():
        if not feeder:
            return jerr("Feeder not running", 503)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jerr("Send a JSON body")
        text, number = body.get("text"), body.get("number")
        if (text is None) == (number is None):
            return jerr("Send exactly one of 'text' or 'number'")

        def small_line(key):
            v = str(body.get(key) or "").strip()
            return v[:13] or None  # the device's small lines hold ~13 chars

        tl, br = small_line("tl"), small_line("br")
        if text is not None:
            if not isinstance(text, str) or not text.strip():
                return jerr("'text' must be a non-empty string")
            if body.get("pair") or body.get("sym"):
                return jerr("'pair' and 'sym' only apply to numbers")
            text = text.strip()
            if len(text) > SLOTS:
                return jerr(f"The display has {SLOTS} character slots; "
                            f"'{text}' is {len(text)} characters. Use tl/br "
                            "for longer words (about 13 characters each).")
            path = show_text_path(text, tl=tl, br=br)
            slotargs = {"text": text}
        else:
            num = str(number).strip()
            if not re.fullmatch(r"-?\d+(\.\d+)?", num):
                return jerr("'number' must be a number, e.g. 42 or -1.5")
            pair = str(body.get("pair") or "").strip().upper() or None
            if pair and not _PAIR_RE.fullmatch(pair):
                return jerr("'pair' must look like BTC/USD (1-4 characters "
                            "each side)")
            sym = str(body.get("sym") or "").strip() or None
            if sym and len(sym) != 1:
                return jerr("'sym' must be a single character, e.g. $")
            capacity = SLOTS - (1 if pair else 0)
            if len(num) > capacity:
                return jerr(f"'{num}' needs {len(num)} slots but only "
                            f"{capacity} fit"
                            + (" next to that pair" if pair else "")
                            + ". Shorten or round the number.")
            path = show_number_path(num, sym=sym, pair=pair, tl=tl, br=br)
            slotargs = {"number": num, "sym": sym, "pair": pair}
        frame = {"name": "agent_show", "label": "Agent frame",
                 "source": "agent", "path": path,
                 "slotargs": {k: v for k, v in slotargs.items()
                              if v is not None}}
        ok, msg = feeder.request_custom(frame)
        if not ok:
            return jerr(msg, 409)
        eta = int(feeder.client.seconds_until_ready()) if feeder.client else 0
        return jsonify({"ok": True,
                        "queued": {"path": path,
                                   "slots": preview_slots(
                                       **frame["slotargs"])},
                        "eta_s": max(eta, 2), "note": msg})

    @app.post("/agent/frame")
    @require_agent
    def agent_frame():
        if not feeder:
            return jerr("Feeder not running", 503)
        body = request.get_json(silent=True) or {}
        fid = str(body.get("frame_id") or "").strip()
        if not fid:
            return jerr("Send {\"frame_id\": \"<id>\"} - GET /agent/frames "
                        "lists the ids")
        if fid not in FRAME_DEFS:
            return jerr(f"Unknown frame_id '{fid}'. GET /agent/frames lists "
                        "the valid ids.")
        try:
            names = {f["name"]
                     for f in build_frames(store.config, all_frames=True)}
        except Exception:
            names = set()
        if fid not in names:
            return jerr(f"'{fid}' isn't available right now - its source is "
                        "disabled or has no data. GET /agent/frames shows "
                        "what is available.")
        ok, msg = feeder.request_test(fid)
        if not ok:
            return jerr(msg, 409)
        return jsonify({"ok": True, "frame_id": fid, "note": msg})

    @app.post("/agent/rotation")
    @require_agent
    def agent_rotation():
        body = request.get_json(silent=True) or {}
        frames_in = body.get("frames")
        if not isinstance(frames_in, list) or not frames_in:
            return jerr("Send {\"frames\": [\"<frame_id>\", ...]} with at "
                        "least one id - GET /agent/frames lists them")
        bad = [f for f in frames_in
               if not isinstance(f, str) or f not in FRAME_DEFS]
        if bad:
            return jerr("Unknown frame ids: " + ", ".join(map(str, bad))
                        + ". GET /agent/frames lists the valid ids.")
        try:
            cfg, _ = validate_config({"rotation": frames_in}, store.config,
                                     strict=True)
        except ConfigError as e:
            return jerr(str(e))
        store.save_config(cfg)
        if feeder:
            feeder.notify_config_changed()
        try:
            buildable = {f["name"] for f in build_frames(cfg)}
        except Exception:
            buildable = set()
        inactive = [f for f in cfg["rotation"] if f not in buildable]
        return jsonify({
            "ok": True, "rotation": cfg["rotation"], "inactive": inactive,
            "note": "Saved. The clock picks it up on its next write."
                    + (" Frames listed in 'inactive' won't show until their "
                       "source is enabled in the app." if inactive else "")})

    @app.get("/openapi.json")
    def openapi_json():
        return jsonify(openapi_spec())

    # ------------------------------------------- umbrelOS widget -------- #

    @app.get("/widgets/stats")
    def widgets_stats():
        """The umbrelOS home-screen widget (four-stats shape). umbrelOS
        fetches this SERVER-SIDE at service:port with no cookies and no
        app-proxy session, so this route is deliberately UNAUTHENTICATED:
        read-only, no config echo, no secret of any kind, and it never
        errors - an unavailable value degrades to a placeholder. Data comes
        from the caches the sources already keep (~5 min TTLs, synthetic
        when BC_OFFLINE=1), so a 1-minute widget refresh never hammers any
        upstream."""
        price_text, ccy = "—", "USD"
        height_text, fee_text = "—", "—"
        clock_text, clock_sub = "Idle", "0 stats"
        try:
            cfg = store.config
            popt = ((cfg.get("sources") or {}).get("price")
                    or {}).get("options") or {}
            ccy = str(popt.get("currency") or "USD").upper()
            price = sources.btc_price(ccy)
            if price:
                price_text = f"{price:,.0f}"
            net = sources.network_snapshot()
            if net.get("height") is not None:
                height_text = f"{net['height']:,}"
            if net.get("fee_fast") is not None:
                fee_text = str(net["fee_fast"])
            n = len(sources.rotation_ids(cfg))
            clock_text = "Live" if (feeder and feeder.driving) else "Idle"
            clock_sub = f"{n} stat" + ("" if n == 1 else "s")
        except Exception:
            log.warning("widget stats degraded to placeholders",
                        exc_info=True)
        return jsonify({
            "type": "four-stats",
            "refresh": "1m",
            "link": "",
            "items": [
                {"title": "BTC price", "text": price_text, "subtext": ccy},
                {"title": "Block height", "text": height_text,
                 "subtext": "blocks"},
                {"title": "Fees", "text": fee_text, "subtext": "sat/vB"},
                {"title": "Clock", "text": clock_text,
                 "subtext": clock_sub},
            ],
        })

    return app


# --------------------------------------------------------------------------- #
# Selfcheck (offline; no docker, no clock, no internet)
# --------------------------------------------------------------------------- #

def selfcheck():
    import json
    import tempfile

    sources.set_offline(True)
    results = []

    def check(name, fn):
        try:
            fn()
            results.append((name, True, ""))
            print(f"  PASS  {name}")
        except Exception as e:
            results.append((name, False, repr(e)))
            print(f"  FAIL  {name}: {e!r}")

    data_dir = tempfile.mkdtemp(prefix="blockclock-selfcheck-")
    store = Store(data_dir)
    feeder = Feeder(store)          # constructed, never started
    app = create_app(store, feeder)
    app.config["TESTING"] = True
    c = app.test_client()

    def t_routes():
        rules = {r.rule for r in app.url_map.iter_rules()}
        for want in ("/", "/healthz", "/api/state", "/api/discover",
                     "/api/connect", "/api/disconnect", "/api/sources",
                     "/api/config", "/api/preview", "/api/test",
                     "/api/geocode", "/api/access", "/api/access-token",
                     "/agent/state", "/agent/frames", "/agent/show",
                     "/agent/frame", "/agent/rotation", "/openapi.json",
                     "/widgets/stats"):
            assert want in rules, f"route {want} missing"

    def t_healthz():
        r = c.get("/healthz")
        assert r.status_code == 200 and r.get_json()["ok"] is True

    def t_state_shape():
        d = c.get("/api/state").get_json()
        for k in ("connected", "running", "config", "last_frame",
                  "next_write_in_s", "suggested_subnet"):
            assert k in d, f"state missing {k}"
        assert d["connected"] is None and d["running"] is False
        assert d["config"]["write_interval_s"] >= MIN_WRITE_INTERVAL_S

    ALL_SOURCES = ["price", "network", "brk", "node", "weather", "macro",
                   "space", "novelty", "shopify", "btcpay"]

    def t_registry():
        # every module registered, every frame id unique + indexed
        assert list(sources.REGISTRY) == ALL_SOURCES, list(sources.REGISTRY)
        seen = set()
        for sid, spec in sources.REGISTRY.items():
            assert callable(spec["builder"]), sid
            assert spec["frames"], sid
            assert isinstance(spec["options_schema"], dict), sid
            for fid, flabel in spec["frames"]:
                assert fid not in seen, fid
                seen.add(fid)
                assert FRAME_DEFS[fid] == (sid, flabel), fid
                assert frame_category(fid) == spec["category"], fid
        assert seen == set(FRAME_DEFS)

    def t_sources_catalogue():
        d = c.get("/api/sources").get_json()
        ids = [s["id"] for s in d["sources"]]
        assert ids == ALL_SOURCES, ids
        assert "USD" in d["currencies"] and "AUD" in d["currencies"]
        cat_ids = [cc["id"] for cc in d["categories"]]
        assert cat_ids and len(cat_ids) == len(set(cat_ids))
        assert "analytics" in cat_ids, cat_ids
        for s in d["sources"]:
            assert s["category"] in cat_ids, s["id"]
            assert s["description"], s["id"]
            for fr in s["frames"]:
                assert fr["id"] in FRAME_DEFS, fr
                assert fr["label"] and fr["category"] == s["category"], fr

    def t_preview_default():
        d = c.get("/api/preview").get_json()
        frames = d["frames"]
        names = [f["name"] for f in frames]
        # defaults: price (2 frames) + network (3 stats)
        for want in ("btc_price", "sats_per_unit", "block_height", "fees",
                     "halving"):
            assert want in names, f"{want} missing from {names}"
        for f in frames:
            assert len(f["slots"]) == 7, f
            assert f["color"], f
            assert f["path"].startswith("/api/show/"), f

    def t_slot_rules():
        # pair eats slot 0
        cells = preview_slots(number=65000, pair="BTC/USD")
        assert cells[0] == "/BTC/USD", cells
        # a non-currency pair (a unit) renders exactly the same way
        cells = preview_slots(number=904, pair="EH/S")
        assert cells[0] == "/EH/S", cells
        assert "".join(cells[1:]).strip() == "904", cells
        # sym silently dropped when no slot is free
        cells = preview_slots(number="1234567", sym="$")
        assert "$" not in cells and "".join(cells) == "1234567", cells
        # sym kept when it fits
        cells = preview_slots(number="123", sym="$")
        assert "$" in cells, cells
        # digits overflow: rightmost digits win
        cells = preview_slots(number="123456789")
        assert "".join(cells) == "3456789", cells
        # text truncated to capacity
        cells = preview_slots(text="melbourne")
        assert "".join(cells) == "MELBOUR", cells
        # pair + long number: only 6 digit slots remain
        cells = preview_slots(number="16400000", pair="BTC/JPY")
        assert "".join(cells[1:]) == "400000", cells

    def t_config_rejects_fast_interval():
        r = c.post("/api/config", json={"write_interval_s": 30})
        assert r.status_code == 400, r.get_json()
        assert "65" in r.get_json()["error"]

    def t_config_rejects_bad_currency():
        r = c.post("/api/config", json={
            "sources": {"price": {"enabled": True,
                                  "options": {"currency": "DOGE"}}}})
        assert r.status_code == 400, r.get_json()

    def t_config_roundtrip():
        payload = {
            "write_interval_s": 90,
            "sources": {
                "price": {"enabled": True, "options": {"currency": "AUD"}},
                "network": {"enabled": True,
                            "options": {"stats": ["block_height", "fees"]}},
                "weather": {"enabled": True,
                            "options": {"city": "Brisbane", "units": "C",
                                        "show_condition": True}},
            },
            "rotation": ["weather_temp", "btc_price", "block_height",
                         "bogus_frame", "btc_price", "fees",
                         "sats_per_unit", "weather_condition"],
        }
        r = c.post("/api/config", json=payload)
        assert r.status_code == 200, r.get_json()
        cfg = r.get_json()["config"]
        assert cfg["write_interval_s"] == 90
        assert cfg["sources"]["price"]["options"]["currency"] == "AUD"
        # geocode resolved (synthetic offline)
        assert cfg["sources"]["weather"]["options"]["lat"] is not None
        # rotation: unknown dropped, dupes deduped
        assert cfg["rotation"] == ["weather_temp", "btc_price",
                                   "block_height", "fees", "sats_per_unit",
                                   "weather_condition"]
        # persisted to disk
        with open(os.path.join(data_dir, "config.json")) as f:
            on_disk = json.load(f)
        assert on_disk["write_interval_s"] == 90

    def t_preview_follows_rotation():
        d = c.get("/api/preview").get_json()
        names = [f["name"] for f in d["frames"]]
        assert names[0] == "weather_temp", names
        assert "weather_condition" in names, names
        wt = next(f for f in d["frames"] if f["name"] == "weather_temp")
        assert len(wt["slots"]) == 7

    def t_geocode_typeahead():
        # offline-safe: synthetic matches with the full trimmed shape
        d = c.get("/api/geocode?q=Testville").get_json()
        assert d["ok"] is True and isinstance(d["results"], list), d
        assert d["results"], d
        for m in d["results"]:
            for k in ("name", "admin1", "country", "country_code",
                      "latitude", "longitude"):
                assert k in m, (k, m)
            assert isinstance(m["latitude"], float), m
            assert isinstance(m["longitude"], float), m
        # a blank query is just an empty list - never an error
        d = c.get("/api/geocode").get_json()
        assert d["ok"] is True and d["results"] == [], d
        d = c.get("/api/geocode?q=%20%20").get_json()
        assert d["ok"] is True and d["results"] == [], d

    def t_weather_pick_passthrough():
        # a typeahead pick (explicit lat/lon/place) is stored verbatim: the
        # save resolves to exactly that place, no geocode-first-result guess
        r = c.post("/api/config", json={"sources": {"weather": {
            "enabled": True, "options": {
                "city": "Springfield", "units": "C",
                "lat": 39.8017, "lon": -89.6437,
                "place": "Springfield, US"}}}})
        assert r.status_code == 200, r.get_json()
        wopt = store.config["sources"]["weather"]["options"]
        assert wopt["lat"] == 39.8017 and wopt["lon"] == -89.6437, wopt
        assert wopt["place"] == "Springfield, US", wopt
        # the public config echoes it (the UI's "Weather for:" note)
        echo = r.get_json()["config"]["sources"]["weather"]["options"]
        assert echo["lat"] == 39.8017 and echo["place"] == "Springfield, US"
        # out-of-range coordinates are ignored -> geocode fallback wins
        r = c.post("/api/config", json={"sources": {"weather": {
            "enabled": True, "options": {
                "city": "Elsewhere", "units": "C",
                "lat": 999, "lon": -89.6437}}}})
        assert r.status_code == 200, r.get_json()
        wopt = store.config["sources"]["weather"]["options"]
        assert wopt["lat"] == -27.47, wopt  # synthetic geocode result
        # restore the roundtrip city (a plain name still geocodes at save)
        r = c.post("/api/config", json={"sources": {"weather": {
            "enabled": True, "options": {"city": "Brisbane", "units": "C",
                                         "show_condition": True}}}})
        assert r.status_code == 200, r.get_json()
        assert store.config["sources"]["weather"]["options"]["lat"] \
            is not None

    def t_preview_candidate_jpy():
        r = c.post("/api/preview", json={"config": {
            "sources": {"price": {"enabled": True,
                                  "options": {"currency": "JPY"}},
                        "network": {"enabled": False, "options": {"stats": []}},
                        "weather": {"enabled": False, "options": {}}}}})
        d = r.get_json()
        names = [f["name"] for f in d["frames"]]
        assert names and "btc_price" in names, names
        # 8-digit JPY price must NOT be truncated: shown in thousands
        bp = next(f for f in d["frames"] if f["name"] == "btc_price")
        assert "x+1000" in bp["path"] or "x%201000" in bp["path"] \
            or "x 1000" in bp["path"], bp["path"]

    def t_discover_validation():
        r = c.post("/api/discover", json={"subnet": "not-a-subnet"})
        assert r.status_code == 400
        r = c.post("/api/discover", json={"subnet": "10.0.0.0/8"})
        assert r.status_code == 400 and "large" in r.get_json()["error"]
        hosts = discovery.parse_subnet("192.168.1.0/24")
        assert len(hosts) == 254 and hosts[0] == "192.168.1.1" \
            and hosts[-1] == "192.168.1.254"
        assert discovery.parse_subnet("192.168.7") == \
            discovery.parse_subnet("192.168.7.0/24")

    def t_connect_validation():
        r = c.post("/api/connect", json={})
        assert r.status_code == 400
        r = c.post("/api/connect", json={"ip": "192.168.1.0/24"})
        assert r.status_code == 400

    def t_test_requires_clock():
        ok, msg = feeder.request_test(None)
        assert ok is False and "No clock" in msg, (ok, msg)
        r = c.post("/api/test", json={})
        assert r.status_code == 409

    def t_index_served():
        r = c.get("/")
        assert r.status_code == 200 and b"BlockClock" in r.data

    # ----------------------------------------------- shopify (merchant) -- #

    SECRET = "shpat_selfcheck9876543210abcdef1234"
    ALL_SHOPIFY = list(SHOPIFY_FRAMES)

    def t_shopify_catalogue():
        d = c.get("/api/sources").get_json()
        sh = next(s for s in d["sources"] if s["id"] == "shopify")
        assert sh.get("advanced") is True
        assert sh["options"]["token_set"] is False
        assert sh["options"]["token_hint"] == ""
        assert "token" not in sh["options"], sh["options"]
        ids = [f["id"] for f in sh["frames"]]
        assert ids == ALL_SHOPIFY, ids
        assert sh["options_schema"]["currency"]["default"] == "AUD"

    def t_shopify_save_and_redact():
        r = c.post("/api/config", json={"sources": {"shopify": {
            "enabled": True, "options": {
                "shop_domain": "selfcheck.myshopify.com", "token": SECRET,
                "currency": "AUD", "daily_goal": 2000, "tz_offset_hours": 10,
                "frames": ALL_SHOPIFY}}}})
        assert r.status_code == 200, r.get_json()
        # stored on disk (the user's own box)...
        assert store.config["sources"]["shopify"]["options"]["token"] \
            == SECRET
        # ...but the response must not contain it - only set/hint
        assert SECRET.encode() not in r.data
        opts = r.get_json()["config"]["sources"]["shopify"]["options"]
        assert opts["token_set"] is True and "token" not in opts
        assert opts["token_hint"].endswith(SECRET[-4:])
        assert SECRET not in opts["token_hint"]

    def t_token_never_in_any_get():
        # grep every GET response for the raw token - it must never appear
        for path in ("/api/state", "/api/sources", "/api/preview"):
            r = c.get(path)
            assert SECRET.encode() not in r.data, path
        opts = c.get("/api/state").get_json()[
            "config"]["sources"]["shopify"]["options"]
        assert opts["token_set"] is True and "token" not in opts
        sh = next(s for s in c.get("/api/sources").get_json()["sources"]
                  if s["id"] == "shopify")
        assert sh["options"]["token_set"] is True
        assert "token" not in sh["options"]

    def t_shopify_offline_frames():
        d = c.get("/api/preview").get_json()
        by_name = {f["name"]: f for f in d["frames"]}
        for want in ALL_SHOPIFY:
            assert want in by_name, (want, sorted(by_name))
            assert len(by_name[want]["slots"]) == 7, by_name[want]
        # synthetic: $1284.50 of $2000 goal = 64% -> amber money frames
        goal = by_name["goal"]
        assert "".join(goal["slots"]).strip().rstrip("%").endswith("64"), goal
        assert goal["color"] == by_name["revenue_today"]["color"]
        # revenue_sats stays within 7 digit slots (compact-sats rule)
        digits = "".join(by_name["revenue_sats"]["slots"]).strip()
        assert len(digits) <= 7 and digits.isdigit(), digits
        # last_city fits the display
        assert "".join(by_name["last_city"]["slots"]).strip() == "BRISBAN"

    def t_token_kept_when_blank():
        # a save with the token field blank/absent KEEPS the stored token
        r = c.post("/api/config", json={"sources": {"shopify": {
            "enabled": True, "options": {
                "shop_domain": "selfcheck.myshopify.com", "token": "",
                "frames": ALL_SHOPIFY}}}})
        assert r.status_code == 200, r.get_json()
        assert store.config["sources"]["shopify"]["options"]["token"] \
            == SECRET
        r = c.post("/api/config", json={"sources": {"shopify": {
            "enabled": True,
            "options": {"shop_domain": "selfcheck.myshopify.com",
                        "frames": ALL_SHOPIFY}}}})
        assert r.status_code == 200
        assert store.config["sources"]["shopify"]["options"]["token"] \
            == SECRET

    def t_shopify_validate_reject():
        # a bad token+domain pair is rejected and NOTHING is stored/enabled
        real = sources.shopify_validate
        sources.shopify_validate = \
            lambda d_, t_, timeout=10: (False, "Shopify rejected that token")
        try:
            r = c.post("/api/config", json={"sources": {"shopify": {
                "enabled": True, "options": {
                    "shop_domain": "selfcheck.myshopify.com",
                    "token": "shpat_badtoken00000000000000000000"}}}})
        finally:
            sources.shopify_validate = real
        assert r.status_code == 400, r.get_json()
        assert "rejected" in r.get_json()["error"]
        assert store.config["sources"]["shopify"]["options"]["token"] \
            == SECRET
        assert "badtoken" not in json.dumps(store.config)

    def t_shopify_domain_rules():
        r = c.post("/api/config", json={"sources": {"shopify": {
            "enabled": True, "options": {
                "shop_domain": "example.com", "frames": ALL_SHOPIFY}}}})
        assert r.status_code == 400
        assert "myshopify.com" in r.get_json()["error"]

    def t_events_cold_start_and_preempt():
        # simulated cycles: hand-fed snapshots drive detection exactly like
        # the run loop does (same _next_shopify_event the loop calls)
        assert not store.state.get("shopify_day")  # genuinely cold
        snap_a = {"revenue": 500.0, "order_count": 6, "units": 9,
                  "max_order": 200.0, "latest_id": 6, "latest_total": 90.0,
                  "latest_city": "Perth", "order_ids": [1, 2, 3, 4, 5, 6]}
        snap_b = dict(snap_a, revenue=650.0, order_count=7, latest_id=7,
                      latest_total=150.0, latest_city="Hobart",
                      order_ids=[1, 2, 3, 4, 5, 6, 7])
        ecfg = {"sources": {"shopify": {"enabled": True, "options": {
            "shop_domain": "selfcheck.myshopify.com", "token": SECRET,
            "currency": "AUD", "daily_goal": 2000, "tz_offset_hours": 10,
            "frames": ["revenue_today"], "flash_on_sale": True}}}}
        real = sources.shopify_snapshot
        feed = {"snap": snap_a}
        sources.shopify_snapshot = lambda *a, **k: dict(feed["snap"])
        try:
            # COLD START: seeds ids + passed milestones, fires NOTHING
            assert feeder._next_shopify_event(ecfg) is None
            st = store.state
            assert st["shopify_seen_ids"] == [1, 2, 3, 4, 5, 6]
            assert st["shopify_milestones_hit"] == [1, 5]
            assert st["pending_events"] == []
            assert st["shopify_day"]
            # one NEW order -> a sale alert PREEMPTS the rotation this window
            feed["snap"] = snap_b
            ev = feeder._next_shopify_event(ecfg)
            assert ev and ev["name"] == "sale_alert", ev
            assert ev["slotargs"]["color"] == "flash"
            assert "SALE" in ev["path"] and "Hobart" in ev["path"]
            cells = preview_slots(**{k: v for k, v in
                                     ev["slotargs"].items() if k != "color"})
            assert len(cells) == 7
            # drained: next cycle resumes rotation (no event)
            assert feeder._next_shopify_event(ecfg) is None
            # 3 new orders at once COALESCE into one frame; crossing the
            # 10-order milestone queues a second event behind it
            snap_c = dict(snap_b, revenue=900.0, order_count=10,
                          latest_id=10, latest_total=80.0,
                          latest_city="Cairns",
                          order_ids=list(range(1, 11)))
            feed["snap"] = snap_c
            ev = feeder._next_shopify_event(ecfg)
            assert ev["name"] == "sale_alert_multi", ev
            assert "".join(preview_slots(number=3)).strip() == "3"
            ev = feeder._next_shopify_event(ecfg)
            assert ev["name"] == "milestone_count", ev
            assert ev["slotargs"]["color"] == "yellow_1"
            assert feeder._next_shopify_event(ecfg) is None
            # beating the all-time daily record fires once
            store.update_state(shopify_record_revenue=850.0,
                               shopify_record_seen=False)
            ev = feeder._next_shopify_event(ecfg)
            assert ev and ev["name"] == "milestone_record", ev
            assert feeder._next_shopify_event(ecfg) is None
            # a Shopify failure = no events this cycle, never a crash
            def boom(*a, **k):
                raise RuntimeError("boom")
            sources.shopify_snapshot = boom
            assert feeder._next_shopify_event(ecfg) is None
        finally:
            sources.shopify_snapshot = real

    def t_shopify_rollover():
        store.update_state(shopify_day="2000-01-01",
                           shopify_last_revenue=900.0,
                           shopify_record_revenue=850.0,
                           shopify_seen_ids=[1], shopify_milestones_hit=[1],
                           shopify_record_seen=True)
        feeder._rollover_if_needed({"tz_offset_hours": 10})
        st = store.state
        assert st["shopify_day"] != "2000-01-01"
        assert st["shopify_seen_ids"] == []
        assert st["shopify_milestones_hit"] == []
        assert st["shopify_record_seen"] is False
        assert st["shopify_record_revenue"] == 900.0  # yesterday won

    def t_shopify_clear_token():
        r = c.post("/api/config", json={"sources": {"shopify": {
            "enabled": False, "options": {"clear_token": True}}}})
        assert r.status_code == 200, r.get_json()
        assert store.config["sources"]["shopify"]["options"]["token"] == ""
        sh = next(s for s in c.get("/api/sources").get_json()["sources"]
                  if s["id"] == "shopify")
        assert sh["options"]["token_set"] is False
        assert sh["options"]["token_hint"] == ""

    # ------------------------------------------- the full library -------- #

    # one distinct sentinel per credential in the app; every GET/echo is
    # grepped for every one of them
    SEC = {
        "shopify": "shpat_libcheck9876543210abcdef1234",
        "bitaroo": "brtk_libcheckAAAABBBBCCCCDDDD0001",
        "core": "corepass_libcheck_S3CR3T_000042",
        "lnd": "0201036c6e640247deadbeefdeadbeefdeadbeefdead",
        "btcpay": "btcpk_libcheck_FFFFEEEEDDDD0007",
        "fred": "fredkey_libcheck_1234567890abcd",
    }

    def t_library_full_enable():
        # switch on EVERY source with (dummy) creds; save must succeed and
        # store every secret on disk while echoing none of them
        payload = {"sources": {
            "price": {"enabled": True,
                      "options": {"exchange": "kraken", "currency": "USD",
                                  "bitaroo_api_key": SEC["bitaroo"]}},
            "network": {"enabled": True,
                        "options": {"stats": list(NETWORK_STATS)}},
            "brk": {"enabled": True, "options": {}},  # series defaults to all
            "node": {"enabled": True,
                     "options": {"base_url": "http://umbrel.local:3006",
                                 "core_host": "umbrel.local",
                                 "core_port": 8332, "core_user": "umbrel",
                                 "core_pass": SEC["core"],
                                 "lnd_host": "umbrel.local", "lnd_port": 8080,
                                 "lnd_macaroon": SEC["lnd"]}},
            "weather": {"enabled": True,
                        "options": {"city": "Brisbane", "units": "C",
                                    "show_condition": True}},
            "macro": {"enabled": True,
                      "options": {"forex_base": "USD", "forex_quote": "AUD",
                                  "fred_api_key": SEC["fred"]}},
            "space": {"enabled": True,
                      "options": {"countdown_label": "HALVING",
                                  "countdown_date": "2028-03-26"}},
            "novelty": {"enabled": True,
                        "options": {"github_repo": "bitcoin/bitcoin"}},
            "shopify": {"enabled": True,
                        "options": {"shop_domain": "selfcheck.myshopify.com",
                                    "token": SEC["shopify"],
                                    "currency": "AUD", "daily_goal": 2000,
                                    "frames": ALL_SHOPIFY}},
            "btcpay": {"enabled": True,
                       "options": {"base_url": "https://btcpay.example.com",
                                   "store_id": "store1",
                                   "api_key": SEC["btcpay"]}},
        }}
        r = c.post("/api/config", json=payload)
        assert r.status_code == 200, r.get_json()
        scfg = store.config["sources"]
        assert scfg["price"]["options"]["bitaroo_api_key"] == SEC["bitaroo"]
        assert scfg["node"]["options"]["core_pass"] == SEC["core"]
        assert scfg["node"]["options"]["lnd_macaroon"] == SEC["lnd"]
        assert scfg["macro"]["options"]["fred_api_key"] == SEC["fred"]
        assert scfg["shopify"]["options"]["token"] == SEC["shopify"]
        assert scfg["btcpay"]["options"]["api_key"] == SEC["btcpay"]
        for name, val in SEC.items():
            assert val.encode() not in r.data, name

    def t_offline_preview_every_frame():
        # GET /api/preview is the library view: EVERY frame of every enabled
        # source renders offline, and every one obeys the 7-slot rules
        d = c.get("/api/preview").get_json()
        frames = d["frames"]
        names = {f["name"] for f in frames}
        missing = set(FRAME_DEFS) - names
        assert not missing, sorted(missing)
        for f in frames:
            assert f["category"], f["name"]
            assert f["path"].startswith("/api/show/"), f
            assert f["color"], f["name"]
            assert len(f["slots"]) == 7, f
            for i, cell in enumerate(f["slots"]):
                if isinstance(cell, str) and cell.startswith("/"):
                    assert i == 0, f  # a pair only ever eats slot 0
                else:
                    assert isinstance(cell, str) and len(cell) == 1, f

    def t_unit_on_face():
        # unit-on-face frames use the device-native pair (like /BTC/AUD on
        # the price frame): unit in the leading cell, value right-justified
        from sources import network as net_mod
        d = c.get("/api/preview").get_json()
        by = {f["name"]: f for f in d["frames"]}
        hr = by["hashrate"]
        assert "pair=EH%2FS" in hr["path"], hr["path"]
        assert hr["slots"][0] == "/EH/S", hr["slots"]
        assert "".join(hr["slots"][1:]).strip() == "993", hr["slots"]
        for fid in ("fees", "fee_half_hour", "fee_hour", "fee_economy"):
            f = by[fid]
            assert "pair=SAT%2FVB" in f["path"], (fid, f["path"])
            assert f["slots"][0] == "/SAT/VB", (fid, f["slots"])
        fd = by["fees_day"]
        assert "pair=BTC%2FDAY" in fd["path"], fd["path"]
        assert fd["slots"][0] == "/BTC/DAY", fd["slots"]
        assert "".join(fd["slots"][1:]).strip() == "0.25", fd["slots"]
        # judgement calls: single-token units stay as br captions (the
        # device's pair cell renders X/Y stacked; VMB/BTC/F10.7 don't)
        assert "pair" not in by["mempool_vsize"]["path"]
        assert "pair" not in by["ln_capacity"]["path"]
        assert "pair" not in by["solar_flux"]["path"]
        # overflow guard: a 7-digit hashrate can't keep the pair - it falls
        # back to number + br caption and the digits are NEVER truncated
        real = net_mod.SYNTHETIC["hashrate"]
        net_mod.SYNTHETIC["hashrate"] = {"currentHashrate": 1_234_567e18,
                                         "currentDifficulty": 127.5e12}
        try:
            frames = net_mod._network_frames({"stats": []}, None)
            hr2 = next(f for f in frames if f["name"] == "hashrate")
            assert "pair" not in hr2["slotargs"], hr2
            assert "EH%2Fs" in hr2["path"], hr2["path"]  # br caption back
            cells = preview_slots(**{k: v for k, v in
                                     hr2["slotargs"].items() if k != "color"})
            assert "".join(cells).strip() == "1234567", cells
        finally:
            net_mod.SYNTHETIC["hashrate"] = real

    def t_no_external_refs():
        # strict self-containment: the served UI references no external
        # URLs, fonts or CDNs (the embedded display font is a data: URI).
        # A scheme followed by a host character is a real reference; the
        # favicon's SVG xmlns (a namespace id, never fetched) and the bare
        # "http://" in a validation hint string are not.
        import re
        # the "Request a feed" footer link is an intentional user-facing
        # navigation anchor to the public repo's issues (opened in a new tab),
        # NOT a fetched resource - allow it while still forbidding external
        # resource loads (fonts/scripts/styles/images).
        FEEDBACK_LINK = ("https://github.com/bayanimills/blockclock-connect/"
                         "issues/new?labels=feed-request")
        for path in ("/", "/static/style.css", "/static/app.js"):
            body = c.get(path).data.decode("utf-8", "replace")
            cleaned = body.replace("http://www.w3.org/2000/svg", "")
            cleaned = cleaned.replace(FEEDBACK_LINK, "")
            hits = re.findall(r"https?://[A-Za-z0-9]", cleaned)
            assert not hits, (path, hits)

    def t_no_secret_in_any_response():
        # grep every read endpoint + the config echo for every sentinel
        for path in ("/api/state", "/api/sources", "/api/preview"):
            r = c.get(path)
            for name, val in SEC.items():
                assert val.encode() not in r.data, (path, name)
        r = c.post("/api/preview", json={"config": {}})
        for name, val in SEC.items():
            assert val.encode() not in r.data, ("preview-candidate", name)
        r = c.post("/api/config", json={})
        assert r.status_code == 200
        for name, val in SEC.items():
            assert val.encode() not in r.data, ("config-echo", name)
        # ...and the *_set / *_hint replacements are what shows instead
        srcs = {s["id"]: s
                for s in c.get("/api/sources").get_json()["sources"]}
        assert srcs["price"]["options"]["bitaroo_api_key_set"] is True
        assert srcs["node"]["options"]["core_pass_set"] is True
        assert srcs["node"]["options"]["lnd_macaroon_set"] is True
        assert srcs["macro"]["options"]["fred_api_key_set"] is True
        assert srcs["btcpay"]["options"]["api_key_set"] is True
        for sid, key in (("node", "core_pass"), ("node", "lnd_macaroon"),
                         ("btcpay", "api_key"), ("macro", "fred_api_key"),
                         ("price", "bitaroo_api_key")):
            assert key not in srcs[sid]["options"], (sid, key)
            hint = srcs[sid]["options"][f"{key}_hint"]
            assert hint and all(v not in hint for v in SEC.values()), (sid,
                                                                       key)

    def t_secret_keep_and_clear():
        # blank/absent secret keeps the saved one; clear_<key> removes it
        r = c.post("/api/config", json={"sources": {"node": {
            "enabled": True, "options": {"core_pass": ""}}}})
        assert r.status_code == 200, r.get_json()
        assert store.config["sources"]["node"]["options"]["core_pass"] \
            == SEC["core"]
        r = c.post("/api/config", json={"sources": {"node": {
            "enabled": True, "options": {"clear_core_pass": True}}}})
        assert r.status_code == 200
        assert store.config["sources"]["node"]["options"]["core_pass"] == ""
        # LND macaroon untouched by the core clear
        assert store.config["sources"]["node"]["options"]["lnd_macaroon"] \
            == SEC["lnd"]
        r = c.post("/api/config", json={"sources": {"node": {
            "enabled": True, "options": {"core_pass": SEC["core"]}}}})
        assert r.status_code == 200  # restore for later checks

    def t_price_providers():
        from sources.price import EXCHANGES as EXX, get_quote
        # offline: EVERY exchange yields a synthetic quote (previews always
        # render, whatever the user picked) - keyless across the board
        for ex, spec in EXX.items():
            q = get_quote(ex, spec["ccys"][0])
            assert q and q["price"] > 0, ex
        # unknown exchange rejected at save
        r = c.post("/api/config", json={"sources": {"price": {
            "enabled": True,
            "options": {"exchange": "mtgox", "currency": "USD"}}}})
        assert r.status_code == 400, r.get_json()
        # bitaroo is KEYLESS now: enabling it with NO token saves cleanly...
        r = c.post("/api/config", json={"sources": {"price": {
            "enabled": True,
            "options": {"exchange": "bitaroo", "currency": "AUD",
                        "clear_bitaroo_api_key": True}}}})
        assert r.status_code == 200, r.get_json()
        # ...and its price frame builds offline with the BTC/AUD pair
        d = c.get("/api/preview").get_json()
        bp = next(f for f in d["frames"] if f["name"] == "btc_price")
        assert "BTC%2FAUD" in bp["path"], bp["path"]
        # peach quotes AUD (and only what it serves: AUD/EUR/CHF)
        assert EXX["peach"]["ccys"] == ["AUD", "EUR", "CHF"]
        r = c.post("/api/config", json={"sources": {"price": {
            "enabled": True,
            "options": {"exchange": "peach", "currency": "AUD"}}}})
        assert r.status_code == 200, r.get_json()
        d = c.get("/api/preview").get_json()
        bp = next(f for f in d["frames"] if f["name"] == "btc_price")
        assert "BTC%2FAUD" in bp["path"], bp["path"]
        assert "".join(bp["slots"][1:]).strip() == "165000", bp["slots"]
        # live mode: unsupported pairs bail BEFORE any HTTP
        sources.set_offline(False)
        try:
            assert get_quote("peach", "USD") is None
            assert get_quote("btcmarkets", "USD") is None
        finally:
            sources.set_offline(True)
        # restore the exchange (and the stored key) for later checks
        r = c.post("/api/config", json={"sources": {"price": {
            "enabled": True,
            "options": {"exchange": "kraken", "currency": "USD",
                        "bitaroo_api_key": SEC["bitaroo"]}}}})
        assert r.status_code == 200, r.get_json()

    def t_bitaroo_stale_key_config():
        # a pre-existing config with a stale bitaroo_api_key still LOADS,
        # builds frames, keeps the key redacted, and re-saves without any
        # "needs an API token" complaint
        stale = "brtk_stalecheck_00000000000000001"
        d2 = tempfile.mkdtemp(prefix="blockclock-stalekey-")
        with open(os.path.join(d2, "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"sources": {"price": {
                "enabled": True,
                "options": {"exchange": "bitaroo", "currency": "AUD",
                            "bitaroo_api_key": stale}}},
                "rotation": ["btc_price", "au_premium"]}, f)
        st2 = Store(d2)
        assert st2.config["sources"]["price"]["options"][
            "bitaroo_api_key"] == stale
        names = [fr["name"] for fr in build_frames(st2.config)]
        assert names == ["btc_price", "au_premium"], names
        app2 = create_app(st2)
        app2.config["TESTING"] = True
        c2 = app2.test_client()
        r = c2.get("/api/state")
        assert stale.encode() not in r.data
        opts = r.get_json()["config"]["sources"]["price"]["options"]
        assert opts["bitaroo_api_key_set"] is True
        assert "bitaroo_api_key" not in opts, opts
        r = c2.post("/api/config", json={"sources": {"price": {
            "enabled": True,
            "options": {"exchange": "bitaroo", "currency": "AUD"}}}})
        assert r.status_code == 200, r.get_json()
        # the untouched save KEPT the stale key on disk (secret semantics)
        assert st2.config["sources"]["price"]["options"][
            "bitaroo_api_key"] == stale

    def t_au_premium_spread():
        import clock as clock_mod
        from sources import price as price_mod
        d = c.get("/api/preview").get_json()
        by = {f["name"]: f for f in d["frames"]}
        # synthetic: Bitaroo mid 165,050 vs AUD spot 165,000 -> +0.03%,
        # rendered SIGNED and tinted green; spread (300/165050) -> 0.18%
        assert "".join(by["au_premium"]["slots"]).strip() == "+0.03", \
            by["au_premium"]["slots"]
        assert by["au_premium"]["color"] == clock_mod.LED_GREEN
        assert "".join(by["au_spread"]["slots"]).strip() == "0.18", \
            by["au_spread"]["slots"]
        # a discount renders with a minus and goes red
        real = price_mod.SYNTHETIC_BITAROO
        price_mod.SYNTHETIC_BITAROO = dict(real, bid=163_000.0,
                                           ask=163_200.0)
        try:
            frames = price_mod._price_frames({"exchange": "coinbase",
                                              "currency": "USD"}, None)
            byn = {f["name"]: f for f in frames}
            assert byn["au_premium"]["slotargs"]["number"] == "-1.15", byn
            assert byn["au_premium"]["slotargs"]["color"] \
                == clock_mod.LED_RED
        finally:
            price_mod.SYNTHETIC_BITAROO = real
        # either leg missing -> the frames SKIP cleanly, nothing crashes
        price_mod.SYNTHETIC_BITAROO = {"last": None, "high": None,
                                       "low": None, "change_pct": None,
                                       "bid": None, "ask": None}
        try:
            frames = price_mod._price_frames({"exchange": "coinbase",
                                              "currency": "USD"}, None)
            names = {f["name"] for f in frames}
            assert "au_premium" not in names and "au_spread" not in names
        finally:
            price_mod.SYNTHETIC_BITAROO = real

    def t_brk_analytics():
        from sources import brk as brk_mod
        # catalogue: brk sits in the analytics category with every series
        d = c.get("/api/sources").get_json()
        b = next(s for s in d["sources"] if s["id"] == "brk")
        assert b["category"] == "analytics"
        assert [f["id"] for f in b["frames"]] == list(brk_mod.SERIES)
        # every series renders offline within the 7-slot rules
        d = c.get("/api/preview").get_json()
        by = {f["name"]: f for f in d["frames"]}
        for fid in brk_mod.SERIES:
            assert fid in by, fid
            assert len(by[fid]["slots"]) == 7, fid
        # known synthetic maths + HONEST compaction of the big numbers
        assert "".join(by["sats_per_usd"]["slots"]).strip() == "1547"
        assert "".join(by["mvrv"]["slots"]).strip() == "1.23"
        assert "".join(by["days_since_ath"]["slots"]).strip() == "303"
        assert "".join(by["puell"]["slots"]).strip() == "0.76"
        assert "".join(by["nupl"]["slots"]).strip() == "0.18"
        assert "".join(by["supply"]["slots"]).strip() == "20.07"
        assert "M+BTC" in by["supply"]["path"], by["supply"]["path"]
        assert "".join(by["market_cap"]["slots"]).strip() == "1.30"
        assert "%24T" in by["market_cap"]["path"], by["market_cap"]["path"]
        # a bad series name is dropped at save (multi-select validation)...
        r = c.post("/api/config", json={"sources": {"brk": {
            "enabled": True, "options": {"series": ["mvrv", "bogus"]}}}})
        assert r.status_code == 200, r.get_json()
        assert store.config["sources"]["brk"]["options"]["series"] \
            == ["mvrv"]
        # ...and a fetch miss just skips the frame, never crashes
        real = brk_mod.series_value
        brk_mod.series_value = lambda s: None
        try:
            assert brk_mod._brk_frames(
                {"series": list(brk_mod.SERIES)}, None) == []
        finally:
            brk_mod.series_value = real
        # restore every series for later checks
        r = c.post("/api/config", json={"sources": {"brk": {
            "enabled": True,
            "options": {"series": list(brk_mod.SERIES)}}}})
        assert r.status_code == 200

    def t_node_gating_and_urls():
        from sources import node as node_mod
        assert node_mod.have_core({}) is False
        assert node_mod.have_core({"core_host": "h", "core_user": "u",
                                   "core_pass": "p"}) is True
        assert node_mod.have_lnd({"lnd_host": "h"}) is False
        assert node_mod.have_lnd({"lnd_host": "h",
                                  "lnd_macaroon": "ab"}) is True
        # a base_url without a scheme is rejected, nothing half-saved
        r = c.post("/api/config", json={"sources": {"node": {
            "enabled": True, "options": {"base_url": "umbrel.local:3006"}}}})
        assert r.status_code == 400, r.get_json()
        assert store.config["sources"]["node"]["options"]["base_url"] \
            == "http://umbrel.local:3006"

    def t_macro_keyed_frames_honest():
        # FRED frames vanish without the key (nothing is faked), return with it
        r = c.post("/api/config", json={"sources": {"macro": {
            "enabled": True, "options": {"clear_fred_api_key": True}}}})
        assert r.status_code == 200
        names = {f["name"] for f in c.get("/api/preview").get_json()["frames"]}
        assert "spx_index" not in names and "us_10y" not in names
        assert "gold_price" in names and "us_debt" in names \
            and "forex" in names
        r = c.post("/api/config", json={"sources": {"macro": {
            "enabled": True, "options": {"fred_api_key": SEC["fred"]}}}})
        assert r.status_code == 200
        names = {f["name"] for f in c.get("/api/preview").get_json()["frames"]}
        assert "spx_index" in names and "us_10y" in names

    def t_library_maths():
        d = c.get("/api/preview").get_json()
        by = {f["name"]: f for f in d["frames"]}
        # Moscow time: 1e8 / synthetic 108000 USD = 926 sats per USD
        assert "".join(by["moscow_time"]["slots"]).strip() == "926"
        # countdown to 2028-03-26 is a positive day count with its label
        days = "".join(by["countdown"]["slots"]).strip()
        assert days.isdigit() and 0 < int(days) < 10_000, days
        assert "HALVING" in by["countdown"]["path"]
        # % mined derived from synthetic totalbc
        assert "".join(by["pct_mined"]["slots"]).strip() == "95.56"
        # fear & greed carries its classification word
        assert "FEAR" in by["fear_greed"]["path"].upper()

    def t_frames_settings_roundtrip():
        r = c.post("/api/config", json={"frames": {
            "btc_price": {"dwell": 3,
                          "window": {"from_hour": 9, "to_hour": 17}},
            "fees": {"dwell": 1},          # default -> not stored
            "bogus_frame": {"dwell": 5},   # unknown id -> dropped
        }})
        assert r.status_code == 200, r.get_json()
        cfgf = r.get_json()["config"]["frames"]
        assert cfgf == {"btc_price": {"dwell": 3,
                                      "window": {"from_hour": 9,
                                                 "to_hour": 17}}}, cfgf
        # persisted
        assert store.config["frames"] == cfgf
        # malformed settings rejected
        r = c.post("/api/config", json={"frames": {"btc_price":
                                                   {"dwell": 0}}})
        assert r.status_code == 400
        r = c.post("/api/config", json={"frames": {"btc_price": {
            "window": {"from_hour": 25, "to_hour": 4}}}})
        assert r.status_code == 400
        r = c.post("/api/config", json={"frames": {"btc_price": {
            "window": {"from_hour": 9}}}})
        assert r.status_code == 400

    def t_rotation_object_form():
        # rotation entries may be {"id", "dwell", "window"} objects; their
        # settings fold into config["frames"]
        r = c.post("/api/config", json={"rotation": [
            {"id": "moscow_time", "dwell": 2},
            "btc_price",
            {"id": "block_age", "window": {"from_hour": 22, "to_hour": 6}},
            {"id": "moscow_time"},   # duplicate dropped
            "bogus_frame",           # unknown dropped
        ]})
        assert r.status_code == 200, r.get_json()
        cfg2 = r.get_json()["config"]
        assert cfg2["rotation"] == ["moscow_time", "btc_price", "block_age"]
        assert cfg2["frames"]["moscow_time"] == {"dwell": 2}
        assert cfg2["frames"]["block_age"]["window"] == {"from_hour": 22,
                                                         "to_hour": 6}

    def t_strict_rotation_is_the_picker():
        # the feeder's view (POST preview / build_frames default): a frame
        # only appears if its source is enabled AND its id is in rotation
        r = c.post("/api/preview", json={"config": {}})
        names = [f["name"] for f in r.get_json()["frames"]]
        assert names == ["moscow_time", "btc_price", "block_age"], names
        # while the GET library view still shows everything enabled
        all_names = {f["name"]
                     for f in c.get("/api/preview").get_json()["frames"]}
        assert "humans_space" in all_names and "gold_price" in all_names

    def t_dwell_and_window_simulated():
        mk = lambda n: {"name": n, "label": n.upper(), "source": "sim",
                        "path": f"/api/show/number/1?tl={n}",
                        "slotargs": {"number": 1}}
        frames = [mk("a"), mk("b"), mk("c")]
        settings = {"a": {"dwell": 3},
                    "b": {"window": {"from_hour": 9, "to_hour": 17}},
                    "c": {"window": {"from_hour": 22, "to_hour": 6}}}

        def cycle(hour):
            showable = [f for f in frames
                        if feeder._window_ok(settings.get(f["name"]), hour)]
            picked = feeder._pick_rotation_frame(showable, settings, hour)
            return picked["name"] if picked else None

        # window maths, incl. the wrap-around form
        assert feeder._window_ok(settings["b"], 12) is True
        assert feeder._window_ok(settings["b"], 20) is False
        assert feeder._window_ok(settings["c"], 23) is True
        assert feeder._window_ok(settings["c"], 3) is True
        assert feeder._window_ok(settings["c"], 12) is False
        assert feeder._window_ok({"window": {"from_hour": 5,
                                             "to_hour": 5}}, 12) is True
        assert feeder._window_ok(None, 12) is True
        # daytime cycle: a dwells 3 windows, then b, then... (c is asleep)
        feeder.rot_index = 0
        feeder._dwell_name, feeder._dwell_left = None, 0
        seq = [cycle(10) for _ in range(6)]
        assert seq == ["a", "a", "a", "b", "a", "a"], seq
        # night cycle: b is skipped, c wakes up
        feeder.rot_index = 0
        feeder._dwell_name, feeder._dwell_left = None, 0
        seq = [cycle(23) for _ in range(4)]
        assert seq == ["a", "a", "a", "c"], seq
        # everything windowed out -> nothing to show this cycle
        feeder.rot_index = 0
        feeder._dwell_name, feeder._dwell_left = None, 0
        only_b = [f for f in frames if f["name"] == "b"]
        showable = [f for f in only_b
                    if feeder._window_ok(settings.get(f["name"]), 20)]
        assert showable == []

    def t_test_accepts_any_enabled_frame():
        # /api/test may push ANY frame an enabled source can build - even one
        # that is not in the rotation
        cfg = store.config
        cfg["clock"] = {"ip": "192.0.2.1", "model": "BLOCKCLOCK mini",
                        "version": "test"}
        store.save_config(cfg)
        try:
            assert "humans_space" not in store.config["rotation"]
            ok, msg = feeder.request_test("humans_space")
            assert ok is True and "humans_space" in msg, (ok, msg)
            r = c.post("/api/test", json={"frame": "gold_price"})
            assert r.status_code == 200, r.get_json()
            assert "gold_price" in r.get_json()["note"]
        finally:
            cfg = store.config
            cfg["clock"] = None
            store.save_config(cfg)
            feeder._pop_test()  # drop the queued test; nothing is connected

    # ------------------------------------------- agent / API access ----- #

    AGENT = {"token": ""}   # filled in by the enable test, used by the rest

    def bearer(token=None):
        t = AGENT["token"] if token is None else token
        return {"Authorization": f"Bearer {t}"}

    def t_api_access_default_off():
        # off by default, on disk and in every echo; agent endpoints answer
        # 403 to everyone (even a would-be token holder) while disabled
        assert store.config["api_access"] == {"enabled": False, "token": ""}
        echo = c.get("/api/state").get_json()["config"]["api_access"]
        assert echo == {"enabled": False, "token_set": False,
                        "token_hint": ""}, echo
        for path in ("/agent/state", "/agent/frames"):
            assert c.get(path).status_code == 403, path
        for path in ("/agent/show", "/agent/frame", "/agent/rotation"):
            r = c.post(path, json={},
                       headers=bearer("bcc_wrong0000000000000000000000000"))
            assert r.status_code == 403, (path, r.status_code)
        # the schema itself is public (it holds no secret) - the API is not
        assert c.get("/openapi.json").status_code == 200

    def t_api_access_enable_reveal():
        # enabling generates a strong token; the config echo stays redacted;
        # the ONE reveal endpoint returns it
        r = c.post("/api/access", json={"enabled": True})
        assert r.status_code == 200, r.get_json()
        acc = r.get_json()["api_access"]
        assert acc["enabled"] is True and acc["token_set"] is True
        assert "token" not in acc, acc
        d = c.get("/api/access-token").get_json()
        assert d["enabled"] is True
        assert d["token"].startswith("bcc_") and len(d["token"]) >= 40, \
            len(d["token"])
        AGENT["token"] = d["token"]
        assert store.config["api_access"]["token"] == d["token"]
        # the hint never contains the token
        assert AGENT["token"] not in acc["token_hint"]

    def t_agent_auth():
        # no header / wrong token -> 401; the right token -> 200
        assert c.get("/agent/state").status_code == 401
        r = c.get("/agent/state",
                  headers=bearer("bcc_wrong0000000000000000000000000"))
        assert r.status_code == 401
        r = c.get("/agent/state", headers={"Authorization": AGENT["token"]})
        assert r.status_code == 401  # must be the Bearer form
        d = c.get("/agent/state", headers=bearer())
        assert d.status_code == 200, d.get_json()
        body = d.get_json()
        for k in ("connected", "running", "last_frame", "next_write_in_s",
                  "write_interval_s", "rotation"):
            assert k in body, k
        assert body["connected"] is False and body["running"] is False
        assert AGENT["token"].encode() not in d.data

    def t_agent_token_never_leaks():
        # the bearer token must never appear in any general read/echo path
        for path in ("/api/state", "/api/sources", "/api/preview"):
            r = c.get(path)
            assert AGENT["token"].encode() not in r.data, path
        r = c.post("/api/config", json={})
        assert r.status_code == 200
        assert AGENT["token"].encode() not in r.data, "config-echo"
        r = c.post("/api/preview", json={"config": {}})
        assert AGENT["token"].encode() not in r.data, "preview-candidate"
        # and the agent's own responses never carry it either
        for path in ("/agent/state", "/agent/frames"):
            r = c.get(path, headers=bearer())
            assert AGENT["token"].encode() not in r.data, path

    def t_agent_frames_library():
        d = c.get("/agent/frames", headers=bearer()).get_json()
        frames = d["frames"]
        assert {f["id"] for f in frames} == set(FRAME_DEFS)
        for f in frames:
            for k in ("id", "label", "category", "available", "in_rotation"):
                assert k in f, (k, f)
        by = {f["id"]: f for f in frames}
        assert by["btc_price"]["available"] is True  # price is enabled
        rot = sources.rotation_ids(store.config)
        assert all(by[fid]["in_rotation"] for fid in rot)

    def t_agent_show_validation_and_queue():
        cfg = store.config
        cfg["clock"] = {"ip": "192.0.2.1", "model": "BLOCKCLOCK mini",
                        "version": "test"}
        store.save_config(cfg)
        try:
            # not both, not neither
            r = c.post("/agent/show", json={"text": "GM", "number": 1},
                       headers=bearer())
            assert r.status_code == 400, r.get_json()
            r = c.post("/agent/show", json={}, headers=bearer())
            assert r.status_code == 400
            # 7-slot limits enforced with friendly errors
            r = c.post("/agent/show", json={"text": "TOOLONGX"},
                       headers=bearer())
            assert r.status_code == 400 and "7" in r.get_json()["error"]
            r = c.post("/agent/show",
                       json={"number": "1234567", "pair": "BTC/USD"},
                       headers=bearer())
            assert r.status_code == 400  # pair eats a slot -> only 6 left
            r = c.post("/agent/show", json={"number": "12x4"},
                       headers=bearer())
            assert r.status_code == 400
            r = c.post("/agent/show",
                       json={"text": "GM", "pair": "BTC/USD"},
                       headers=bearer())
            assert r.status_code == 400  # pair/sym are number-only
            r = c.post("/agent/show",
                       json={"number": 1, "pair": "TOOLONG/PAIR"},
                       headers=bearer())
            assert r.status_code == 400
            # a good text frame queues (feeder never started: queued only)
            r = c.post("/agent/show", json={"text": "GM", "tl": "from your",
                                            "br": "agent"}, headers=bearer())
            assert r.status_code == 200, r.get_json()
            d = r.get_json()
            assert d["queued"]["path"].startswith("/api/show/text/GM")
            assert len(d["queued"]["slots"]) == 7
            assert d["eta_s"] >= 2 and "rate limit" in d["note"]
            # a good number frame with pair + sym
            r = c.post("/agent/show",
                       json={"number": 165000, "pair": "BTC/AUD"},
                       headers=bearer())
            assert r.status_code == 200, r.get_json()
            d = r.get_json()
            assert d["queued"]["slots"][0] == "/BTC/AUD"
            assert "".join(d["queued"]["slots"][1:]).strip() == "165000"
            # the queued custom frame is the one-shot the feeder will pop
            pending = feeder._pop_custom()
            assert pending and pending["name"] == "agent_show", pending
            assert pending["path"].startswith("/api/show/number/165000")
        finally:
            cfg = store.config
            cfg["clock"] = None
            store.save_config(cfg)
            feeder._pop_custom()

        # without a clock connected: friendly 409, nothing queued
        r = c.post("/agent/show", json={"text": "GM"}, headers=bearer())
        assert r.status_code == 409 and "No clock" in r.get_json()["error"]
        assert feeder._pop_custom() is None

    def t_agent_frame_push():
        cfg = store.config
        cfg["clock"] = {"ip": "192.0.2.1", "model": "BLOCKCLOCK mini",
                        "version": "test"}
        store.save_config(cfg)
        try:
            r = c.post("/agent/frame", json={}, headers=bearer())
            assert r.status_code == 400
            r = c.post("/agent/frame", json={"frame_id": "bogus"},
                       headers=bearer())
            assert r.status_code == 400
            assert "frames" in r.get_json()["error"]
            r = c.post("/agent/frame", json={"frame_id": "btc_price"},
                       headers=bearer())
            assert r.status_code == 200, r.get_json()
            assert "Queued" in r.get_json()["note"]
        finally:
            cfg = store.config
            cfg["clock"] = None
            store.save_config(cfg)
            feeder._pop_test()

    def t_agent_rotation_set():
        before = sources.rotation_ids(store.config)
        r = c.post("/agent/rotation", json={"frames": "btc_price"},
                   headers=bearer())
        assert r.status_code == 400
        r = c.post("/agent/rotation",
                   json={"frames": ["btc_price", "bogus_frame"]},
                   headers=bearer())
        assert r.status_code == 400 and "bogus_frame" in r.get_json()["error"]
        assert sources.rotation_ids(store.config) == before  # unchanged
        r = c.post("/agent/rotation",
                   json={"frames": ["moscow_time", "btc_price", "fees",
                                    "btc_price"]}, headers=bearer())
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert d["rotation"] == ["moscow_time", "btc_price", "fees"]  # deduped
        assert store.config["rotation"] == d["rotation"]  # persisted
        with open(os.path.join(data_dir, "config.json")) as f:
            assert json.load(f)["rotation"] == d["rotation"]

    def t_agent_regenerate_and_disable():
        old = AGENT["token"]
        r = c.post("/api/access", json={"regenerate": True},
                   headers=bearer())
        assert r.status_code == 200
        new = c.get("/api/access-token").get_json()["token"]
        assert new != old and new.startswith("bcc_")
        AGENT["token"] = new
        # the old token stops working immediately; the new one works
        assert c.get("/agent/state",
                     headers=bearer(old)).status_code == 401
        assert c.get("/agent/state", headers=bearer()).status_code == 200
        # disabling turns the API off (403 even with the right token)...
        r = c.post("/api/access", json={"enabled": False})
        assert r.status_code == 200
        assert r.get_json()["api_access"]["enabled"] is False
        assert c.get("/agent/state", headers=bearer()).status_code == 403
        # ...but KEEPS the token, so re-enabling is stable
        d = c.get("/api/access-token").get_json()
        assert d["enabled"] is False and d["token"] == new
        r = c.post("/api/access", json={"enabled": True})
        assert r.status_code == 200
        assert c.get("/agent/state", headers=bearer()).status_code == 200
        # leave it how it began: off (token retained)
        c.post("/api/access", json={"enabled": False})

    def t_openapi_schema():
        r = c.get("/openapi.json")
        assert r.status_code == 200
        spec = json.loads(r.data)  # valid JSON by construction
        assert spec["openapi"].startswith("3."), spec["openapi"]
        scheme = spec["components"]["securitySchemes"]["bearerAuth"]
        assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
        for p in ("/agent/state", "/agent/frames", "/agent/show",
                  "/agent/frame", "/agent/rotation"):
            assert p in spec["paths"], p
        assert set(spec["paths"]) == {"/agent/state", "/agent/frames",
                                      "/agent/show", "/agent/frame",
                                      "/agent/rotation"}  # agent-only
        for path, ops in spec["paths"].items():
            for op in ops.values():
                assert op["security"] == [{"bearerAuth": []}], path
                assert op["summary"] and op["description"], path
        assert spec["servers"][0]["url"] == "/"
        # no token anywhere near the schema
        assert AGENT["token"].encode() not in r.data

    def t_widget_stats():
        # umbrelOS fetches this server-side with NO cookies and NO token:
        # it must answer 200 with no auth of any kind, in the exact
        # four-stats widget shape, offline-safe (synthetic values, never a
        # placeholder-only card here), and leak no secret - checked against
        # every credential sentinel AND the live agent bearer token.
        from sources import network as net_mod
        r = c.get("/widgets/stats")   # no Authorization, no session
        assert r.status_code == 200, r.status_code
        d = r.get_json()
        assert d["type"] == "four-stats", d
        assert d["refresh"] == "1m" and d["link"] == "", d
        items = d["items"]
        assert isinstance(items, list) and len(items) == 4, items
        for it in items:
            assert set(it) == {"title", "text", "subtext"}, it
            for v in it.values():
                assert isinstance(v, str) and v, it
        titles = [it["title"] for it in items]
        assert titles == ["BTC price", "Block height", "Fees",
                          "Clock"], titles
        # offline: the synthetic sources flow through (no "—" placeholders)
        ccy = store.config["sources"]["price"]["options"]["currency"]
        assert items[0]["subtext"] == ccy, items[0]
        assert items[0]["text"] != "—", items[0]
        assert items[1]["subtext"] == "blocks"
        assert items[1]["text"] \
            == f"{net_mod.SYNTHETIC_NETWORK['height']:,}", items[1]
        assert items[2]["subtext"] == "sat/vB"
        assert items[2]["text"] \
            == str(net_mod.SYNTHETIC_NETWORK['fee_fast']), items[2]
        # the feeder was never started here -> Idle, with the rotation size
        assert items[3]["text"] == "Idle", items[3]
        rot_n = len(sources.rotation_ids(store.config))
        assert items[3]["subtext"] \
            == f"{rot_n} stat" + ("" if rot_n == 1 else "s"), items[3]
        # no secret of any kind in the response
        for name, val in SEC.items():
            assert val.encode() not in r.data, name
        assert AGENT["token"], "agent token should exist by this point"
        assert AGENT["token"].encode() not in r.data

    print("blockclock-connect selfcheck (offline, synthetic data)")
    for name, fn in [
        ("routes registered", t_routes),
        ("/healthz", t_healthz),
        ("/api/state shape", t_state_shape),
        ("registry: all modules + unique frame ids", t_registry),
        ("/api/sources catalogue", t_sources_catalogue),
        ("/api/preview default frames", t_preview_default),
        ("7-slot / sym-drop / pair rules", t_slot_rules),
        ("config: interval < 65 rejected", t_config_rejects_fast_interval),
        ("config: bad currency rejected", t_config_rejects_bad_currency),
        ("config: save + geocode + rotation cleanup", t_config_roundtrip),
        ("preview follows saved rotation", t_preview_follows_rotation),
        ("GET /api/geocode: typeahead shape, blank-safe, offline synthetic",
         t_geocode_typeahead),
        ("weather: typeahead lat/lon/place pass through save exactly",
         t_weather_pick_passthrough),
        ("preview candidate: JPY never truncated", t_preview_candidate_jpy),
        ("discovery subnet validation", t_discover_validation),
        ("connect input validation", t_connect_validation),
        ("test push requires a clock", t_test_requires_clock),
        ("frontend served at /", t_index_served),
        ("shopify: catalogue entry (token redacted)", t_shopify_catalogue),
        ("shopify: save validates + response redacts",
         t_shopify_save_and_redact),
        ("shopify: token never in any GET response",
         t_token_never_in_any_get),
        ("shopify: offline frames obey 7-slot rules",
         t_shopify_offline_frames),
        ("shopify: blank token keeps the saved one", t_token_kept_when_blank),
        ("shopify: bad token rejected, nothing stored",
         t_shopify_validate_reject),
        ("shopify: non-myshopify domain rejected", t_shopify_domain_rules),
        ("shopify: events preempt rotation, cold start seeds silently",
         t_events_cold_start_and_preempt),
        ("shopify: daily rollover archives the record", t_shopify_rollover),
        ("shopify: explicit clear removes the token", t_shopify_clear_token),
        ("library: every source enables with creds, none echoed",
         t_library_full_enable),
        ("library: offline preview renders EVERY frame in 7-slot rules",
         t_offline_preview_every_frame),
        ("units on the face: hashrate/fees/fees_day pair, overflow-safe",
         t_unit_on_face),
        ("ui: served HTML/CSS/JS reference no external URLs",
         t_no_external_refs),
        ("library: no secret in any GET/echo (all 6 sentinels)",
         t_no_secret_in_any_response),
        ("library: blank keeps / clear_<key> removes each secret",
         t_secret_keep_and_clear),
        ("price: every exchange quotes offline; keyless bitaroo + peach",
         t_price_providers),
        ("price: stale bitaroo_api_key config loads, redacts, ignores",
         t_bitaroo_stale_key_config),
        ("price: AU premium/spread signed, tinted, skip-clean",
         t_au_premium_spread),
        ("brk: analytics category, every series offline in 7-slot rules",
         t_brk_analytics),
        ("node: core/lnd cred gating + base_url scheme rule",
         t_node_gating_and_urls),
        ("macro: FRED frames off without a key, on with one",
         t_macro_keyed_frames_honest),
        ("library: moscow/countdown/%mined/fear-greed maths",
         t_library_maths),
        ("config: per-frame dwell+window validate and persist",
         t_frames_settings_roundtrip),
        ("config: rotation objects fold dwell/window into frames",
         t_rotation_object_form),
        ("rotation is the picker; GET preview stays the full library",
         t_strict_rotation_is_the_picker),
        ("feeder: dwell + window honoured in a simulated cycle",
         t_dwell_and_window_simulated),
        ("test push accepts any enabled frame id",
         t_test_accepts_any_enabled_frame),
        ("api access: OFF by default, agent endpoints 403, echo redacted",
         t_api_access_default_off),
        ("api access: enable generates token; /api/access-token reveals it",
         t_api_access_enable_reveal),
        ("agent: bearer auth (401 wrong/missing, 200 right)", t_agent_auth),
        ("agent: token never in any GET/echo/agent response",
         t_agent_token_never_leaks),
        ("agent: frames library shape", t_agent_frames_library),
        ("agent: show validates 7-slot rules and queues via the feeder",
         t_agent_show_validation_and_queue),
        ("agent: frame push by id (unknown ids 400)", t_agent_frame_push),
        ("agent: rotation validates, dedupes, persists",
         t_agent_rotation_set),
        ("api access: regenerate rotates, disable keeps token",
         t_agent_regenerate_and_disable),
        ("openapi.json: bearer scheme + the 5 agent paths only",
         t_openapi_schema),
        ("widget: /widgets/stats no-auth four-stats, offline-safe, "
         "no secret", t_widget_stats),
    ]:
        check(name, fn)

    failed = [r for r in results if not r[1]]
    print(f"\n{'FAIL' if failed else 'PASS'}: "
          f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ")

    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())

    data_dir = os.environ.get("DATA_DIR", "/data")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as e:
        log.warning("cannot create DATA_DIR %s: %r (config won't persist)",
                    data_dir, e)

    store = Store(data_dir)
    feeder = Feeder(store)
    app = create_app(store, feeder)
    feeder.start()

    def _handle(signum, _frame):
        log.info("signal %s: restoring clock and shutting down", signum)
        feeder.shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    log.info("BlockClock Connect listening on 0.0.0.0:%d (data: %s)",
             PORT, data_dir)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
