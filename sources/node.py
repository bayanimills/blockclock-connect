"""Your node - the sovereign surface.

Three rungs on the sovereignty ladder, all optional:

  * base_url (default https://mempool.space): the standard Esplora paths,
    pointable at your OWN mempool instance (e.g. http://umbrel.local:3006) -
    the same numbers, no third party in the loop;
  * Bitcoin Core JSON-RPC (host + port + rpcauth user/pass): peers, height,
    mempool size, uptime straight from the node itself;
  * LND REST (host + port + hex READONLY macaroon, self-signed TLS
    tolerated): your channel count and channel balance.

CRED SECURITY: core_pass and lnd_macaroon live only in the saved config on
the user's own box. They go out only in the request to the user's own node,
are NEVER echoed by any API response (redacted to *_set / *_hint), and never
logged - every failure is reduced to an HTTP status or exception class name
by _http_redacted / _core_rpc.
"""

import base64
import json

from clock import LED_GREEN, show_number_path
from sources import (_cached, _frame, _http_json, _http_redacted,
                     compact_sats, is_offline, register_source)
from sources.network import fee_color

DEFAULT_BASE = "https://mempool.space"

SYNTHETIC_NODE = {"height": 908_642, "age_min": 9, "fee": 4,
                  "mempool": 41_250, "peers": 10, "core_height": 908_642,
                  "core_mempool": 41_250, "uptime_days": 45,
                  "lnd_channels": 8, "lnd_balance": 250_000}


def _base(options):
    url = str(options.get("base_url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return DEFAULT_BASE
    return url


def have_core(options):
    """True when a Bitcoin Core RPC connection is fully configured."""
    o = options or {}
    return bool(str(o.get("core_host") or "").strip()
                and str(o.get("core_user") or "").strip()
                and str(o.get("core_pass") or ""))


def have_lnd(options):
    """True when an LND REST connection is fully configured."""
    o = options or {}
    return bool(str(o.get("lnd_host") or "").strip()
                and str(o.get("lnd_macaroon") or "").strip())


# --------------------------------------------------------------------------- #
# Esplora paths against the configured base (identical to mempool.space's)
# --------------------------------------------------------------------------- #

def _esplora(base, path, ttl=120):
    return _cached(f"node:{base}:{path}", ttl,
                   lambda: _http_json(f"{base}{path}"))


# --------------------------------------------------------------------------- #
# Bitcoin Core JSON-RPC (POST + basic auth; a deviation from the app's GET
# pattern, worth its own helper). Never logs anything but a class name.
# --------------------------------------------------------------------------- #

def _core_rpc(options, method):
    host = str(options.get("core_host") or "").strip()
    try:
        port = int(options.get("core_port") or 8332)
    except (TypeError, ValueError):
        port = 8332
    auth = base64.b64encode(
        f"{options.get('core_user')}:{options.get('core_pass')}"
        .encode()).decode()
    body = json.dumps({"jsonrpc": "1.0", "id": "blockclock",
                       "method": method, "params": []}).encode()
    d, _why = _http_redacted(
        f"http://{host}:{port}/", data=body, timeout=10,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/json"})
    if isinstance(d, dict):
        return d.get("result")
    return None


def _core_cached(options, method, ttl=60):
    host = str(options.get("core_host") or "").strip()
    return _cached(f"core:{host}:{method}", ttl,
                   lambda: _core_rpc(options, method))


# --------------------------------------------------------------------------- #
# LND REST (self-signed TLS tolerated; READONLY macaroon in a header)
# --------------------------------------------------------------------------- #

def _lnd_get(options, path, ttl=60):
    host = str(options.get("lnd_host") or "").strip()
    try:
        port = int(options.get("lnd_port") or 8080)
    except (TypeError, ValueError):
        port = 8080
    macaroon = str(options.get("lnd_macaroon") or "").strip()

    def fetch():
        d, _why = _http_redacted(
            f"https://{host}:{port}{path}", timeout=10, insecure=True,
            headers={"Grpc-Metadata-macaroon": macaroon})
        return d if isinstance(d, dict) else None
    return _cached(f"lnd:{host}:{path}", ttl, fetch)


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #

def _node_frames(options, wanted=None):
    def want(fid):
        return wanted is None or fid in wanted

    offline = is_offline()
    frames = []
    add = frames.append
    base = _base(options)

    # ---- Esplora frames against the configured base URL ----
    if want("node_height"):
        h = (SYNTHETIC_NODE["height"] if offline
             else _esplora(base, "/api/blocks/tip/height"))
        try:
            h = int(h)
        except (TypeError, ValueError):
            h = None
        if h is not None:
            add(_frame("node_height",
                       show_number_path(h, tl="my node", br="height"),
                       number=h, color=LED_GREEN))
    if want("node_block_age"):
        age = None
        if offline:
            age = SYNTHETIC_NODE["age_min"]
        else:
            import time
            blocks = _esplora(base, "/api/v1/blocks")
            try:
                age = max(0, int((time.time()
                                  - float(blocks[0]["timestamp"])) / 60))
            except (TypeError, KeyError, IndexError, ValueError):
                pass
        if age is not None and age <= 9_999_999:
            add(_frame("node_block_age",
                       show_number_path(age, tl="my node", br="block age min"),
                       number=age, color=LED_GREEN))
    if want("node_fee"):
        if offline:
            fee = SYNTHETIC_NODE["fee"]
        else:
            fees = _esplora(base, "/api/v1/fees/recommended", ttl=300)
            try:
                fee = int(fees["fastestFee"])
            except (TypeError, KeyError, ValueError):
                fee = None
        if fee is not None:
            add(_frame("node_fee",
                       show_number_path(fee, tl="my node fee", br="sat/vB"),
                       number=fee, color=fee_color(fee)))
    if want("node_mempool"):
        if offline:
            n = SYNTHETIC_NODE["mempool"]
        else:
            mp = _esplora(base, "/api/mempool", ttl=300)
            try:
                n = int(mp["count"])
            except (TypeError, KeyError, ValueError):
                n = None
        if n is not None and n <= 9_999_999:
            add(_frame("node_mempool",
                       show_number_path(n, tl="my mempool",
                                        br="unconfirmed tx"),
                       number=n, color=LED_GREEN))

    # ---- Bitcoin Core (only when fully configured; synthetic offline) ----
    if offline or have_core(options):
        core_specs = [
            ("core_peers", "getconnectioncount", "peers",
             lambda r: int(r), "my core node", "peers"),
            ("core_height", "getblockcount", "core_height",
             lambda r: int(r), "my core node", "height"),
            ("core_mempool", "getmempoolinfo", "core_mempool",
             lambda r: int(r["size"]), "my core node", "mempool tx"),
            ("core_uptime", "uptime", "uptime_days",
             lambda r: int(int(r) / 86_400), "node uptime", "days"),
        ]
        for fid, method, synth_key, extract, tl, br in core_specs:
            if not want(fid):
                continue
            if offline:
                val = SYNTHETIC_NODE[synth_key]
            else:
                result = _core_cached(options, method)
                try:
                    val = extract(result) if result is not None else None
                except (TypeError, KeyError, ValueError):
                    val = None
            if val is not None and val <= 9_999_999:
                add(_frame(fid, show_number_path(val, tl=tl, br=br),
                           number=val, color=LED_GREEN))

    # ---- LND (only when fully configured; synthetic offline) ----
    if offline or have_lnd(options):
        if want("lnd_channels"):
            if offline:
                n = SYNTHETIC_NODE["lnd_channels"]
            else:
                info = _lnd_get(options, "/v1/getinfo")
                try:
                    n = int(info["num_active_channels"])
                except (TypeError, KeyError, ValueError):
                    n = None
            if n is not None and n <= 9_999_999:
                add(_frame("lnd_channels",
                           show_number_path(n, tl="my lightning",
                                            br="channels"),
                           number=n, color=LED_GREEN))
        if want("lnd_balance"):
            if offline:
                sats = SYNTHETIC_NODE["lnd_balance"]
            else:
                bal = _lnd_get(options, "/v1/balance/channels")
                sats = None
                try:
                    local = bal.get("local_balance")
                    sats = int(local["sat"] if isinstance(local, dict)
                               else bal["balance"])
                except (TypeError, KeyError, ValueError):
                    pass
            val, unit = compact_sats(sats)  # never overflows the 7 digits
            if val is not None:
                add(_frame("lnd_balance",
                           show_number_path(val, tl="LN balance",
                                            br=unit or "sats"),
                           number=val, color=LED_GREEN))
    return frames


def _validate(src_out, fail):
    """Post-schema hook: normalise the base URL (bad scheme falls back to the
    public default rather than saving a URL the feeder can't use)."""
    opts = src_out["options"]
    url = str(opts.get("base_url") or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        fail("node.base_url must start with http:// or https://")
        url = DEFAULT_BASE
    opts["base_url"] = url or DEFAULT_BASE


register_source(
    "node",
    label="Your node",
    category="node",
    description="Point the network frames at your OWN mempool instance "
                "(any Esplora-compatible base URL, e.g. your Umbrel's "
                "mempool app), and optionally read Bitcoin Core (JSON-RPC) "
                "and LND (REST, readonly macaroon) directly. Credentials "
                "stay on this device and only ever go to your own node.",
    advanced=True,
    frames=[
        ("node_height", "Block height (my node)"),
        ("node_block_age", "Block age (my node)"),
        ("node_fee", "Fastest fee (my node)"),
        ("node_mempool", "Mempool (my node)"),
        ("core_peers", "Core: peer count"),
        ("core_height", "Core: block height"),
        ("core_mempool", "Core: mempool size"),
        ("core_uptime", "Core: uptime (days)"),
        ("lnd_channels", "LND: active channels"),
        ("lnd_balance", "LND: channel balance"),
    ],
    builder=_node_frames,
    options_schema={
        "base_url": {"type": "text", "label": "Mempool base URL",
                     "default": DEFAULT_BASE,
                     "placeholder": "http://umbrel.local:3006"},
        "core_host": {"type": "text", "label": "Bitcoin Core host",
                      "default": "", "placeholder": "umbrel.local"},
        "core_port": {"type": "number", "label": "Core RPC port",
                      "default": 8332, "min": 1, "max": 65535},
        "core_user": {"type": "text", "label": "Core RPC user",
                      "default": ""},
        "core_pass": {"type": "password", "label": "Core RPC password",
                      "default": ""},
        "lnd_host": {"type": "text", "label": "LND REST host",
                     "default": "", "placeholder": "umbrel.local"},
        "lnd_port": {"type": "number", "label": "LND REST port",
                     "default": 8080, "min": 1, "max": 65535},
        "lnd_macaroon": {"type": "password",
                         "label": "LND readonly macaroon (hex)",
                         "default": ""},
    },
    secrets=("core_pass", "lnd_macaroon"),
    validate=_validate,
)
