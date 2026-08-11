"""Bitcoin network & on-chain stats.

mempool.space is the backbone (same generous keyless tier the app already
uses), joined by blockchain.info's bare-number /q endpoints, Bitnodes'
reachable-node crawl, and alternative.me's Fear & Greed index. Every fetch is
cached at a TTL matched to how fast the number actually moves, and every
frame skips (never crashes) when its source is down.

The 5 LEGACY stats (block_height / fees / halving / difficulty / mempool)
stay gated by options.stats so the existing UI keeps working; every NEW frame
is selected purely via the rotation.
"""

import time

from clock import (LED_AMBER, LED_GREEN, LED_NET_BLUE, LED_RED, SLOTS,
                   show_number_path)
from sources import (CACHE_TTL_S, HALVING_INTERVAL, MEMPOOL_API,
                     NETWORK_STATS, _cached, _fit_line, _frame, _http_json,
                     is_offline, register_source)

BLOCKCHAIN_Q = "https://blockchain.info/q"

SYNTHETIC_NETWORK = {"height": 908_642, "fee_fast": 4, "diff_change": -1.4,
                     "mempool_count": 41_250}
SYNTHETIC = {
    "tip_age_s": 720,                      # 12 minutes since the last block
    "fees": {"fastestFee": 4, "halfHourFee": 3, "hourFee": 2,
             "economyFee": 1},
    "hashrate": {"currentHashrate": 993e18, "currentDifficulty": 127.5e12},
    "mempool": {"count": 41_250, "vsize": 41_000_000},
    "reward_total_fee": 25_000_000,        # sats over 144 blocks = 0.25 BTC
    "pools": [{"name": "Foundry USA", "blockCount": 264},
              {"name": "AntPool", "blockCount": 220},
              {"name": "Other", "blockCount": 616}],
    "ln": {"node_count": 17_420, "channel_count": 48_000,
           "total_capacity": 479_800_000_000},
    "diff_adj": {"difficultyChange": -1.4, "remainingBlocks": 1_775},
    "totalbc": 2_006_812_500_000_000,      # sats mined -> 95.56% of 21M
    "txns_24h": 680_665,
    "block_interval_s": 578,
    "bitnodes": 26_591,
    "fng": {"value": 30, "classification": "Fear"},
}


# --------------------------------------------------------------------------- #
# Cached fetchers (one per upstream endpoint; frames pick fields from them)
# --------------------------------------------------------------------------- #

def network_snapshot():
    """LEGACY snapshot for the original five stats - unchanged behaviour.
    Every key may be None and each frame that reads a None simply doesn't
    render (skip, never crash)."""
    if is_offline():
        return dict(SYNTHETIC_NETWORK)
    height = _cached("tip_height", CACHE_TTL_S,
                     lambda: _http_json(f"{MEMPOOL_API}/blocks/tip/height"))
    fees = _fees()
    diff = _diff_adj()
    mp = _mempool()
    out = {"height": None, "fee_fast": None, "diff_change": None,
           "mempool_count": None}
    try:
        if height is not None:
            out["height"] = int(height)
    except (TypeError, ValueError):
        pass
    try:
        if isinstance(fees, dict) and fees.get("fastestFee") is not None:
            out["fee_fast"] = int(fees["fastestFee"])
    except (TypeError, ValueError):
        pass
    try:
        if isinstance(diff, dict) and diff.get("difficultyChange") is not None:
            out["diff_change"] = float(diff["difficultyChange"])
    except (TypeError, ValueError):
        pass
    try:
        if isinstance(mp, dict) and mp.get("count") is not None:
            out["mempool_count"] = int(mp["count"])
    except (TypeError, ValueError):
        pass
    return out


def _fees():
    if is_offline():
        return dict(SYNTHETIC["fees"])
    return _cached("fees", CACHE_TTL_S,
                   lambda: _http_json(f"{MEMPOOL_API}/v1/fees/recommended"))


def _diff_adj():
    if is_offline():
        return dict(SYNTHETIC["diff_adj"])
    return _cached("diff_adj", CACHE_TTL_S,
                   lambda: _http_json(f"{MEMPOOL_API}/v1/difficulty-adjustment"))


def _mempool():
    if is_offline():
        return dict(SYNTHETIC["mempool"])
    return _cached("mempool", CACHE_TTL_S,
                   lambda: _http_json(f"{MEMPOOL_API}/mempool"))


def _tip_timestamp():
    """Unix time of the chain tip. Cached briefly (120s) so block age stays
    honest; the age itself is recomputed from the wall clock every build."""
    if is_offline():
        return time.time() - SYNTHETIC["tip_age_s"]

    def fetch():
        d = _http_json(f"{MEMPOOL_API}/v1/blocks")
        try:
            return float(d[0]["timestamp"])
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    return _cached("tip_blocks", 120, fetch)


def _hashrate():
    if is_offline():
        return dict(SYNTHETIC["hashrate"])
    return _cached("hashrate", 1800,
                   lambda: _http_json(f"{MEMPOOL_API}/v1/mining/hashrate/3d"))


def _reward_total_fee():
    """Total fees (sats) over the last 144 blocks (~1 day)."""
    if is_offline():
        return SYNTHETIC["reward_total_fee"]

    def fetch():
        d = _http_json(f"{MEMPOOL_API}/v1/mining/reward-stats/144")
        try:
            return int(d["totalFee"])
        except (TypeError, KeyError, ValueError):
            return None
    return _cached("reward_stats", 1800, fetch)


def _pools():
    if is_offline():
        return [dict(p) for p in SYNTHETIC["pools"]]

    def fetch():
        d = _http_json(f"{MEMPOOL_API}/v1/mining/pools/1w")
        pools = d.get("pools") if isinstance(d, dict) else None
        return pools if isinstance(pools, list) and pools else None
    return _cached("pools_1w", 3600, fetch)


def _ln_stats():
    if is_offline():
        return dict(SYNTHETIC["ln"])

    def fetch():
        d = _http_json(f"{MEMPOOL_API}/v1/lightning/statistics/latest")
        latest = d.get("latest") if isinstance(d, dict) else None
        return latest if isinstance(latest, dict) else None
    return _cached("ln_stats", 3600, fetch)


def _q_int(name, path, ttl):
    """blockchain.info /q endpoints: each returns one plain-text number."""
    def fetch():
        d = _http_json(f"{BLOCKCHAIN_Q}/{path}")
        try:
            return int(float(d))
        except (TypeError, ValueError):
            return None
    return _cached(f"bq:{name}", ttl, fetch)


def _totalbc():
    if is_offline():
        return SYNTHETIC["totalbc"]
    return _q_int("totalbc", "totalbc", 1800)


def _txns_24h():
    if is_offline():
        return SYNTHETIC["txns_24h"]
    return _q_int("tx24h", "24hrtransactioncount", 900)


def _block_interval():
    if is_offline():
        return SYNTHETIC["block_interval_s"]
    return _q_int("interval", "interval", 900)


def _bitnodes():
    """Reachable node count. The snapshot URL 302s to the concrete snapshot;
    urllib follows redirects by default - keep the trailing slash."""
    if is_offline():
        return SYNTHETIC["bitnodes"]

    def fetch():
        d = _http_json("https://bitnodes.io/api/v1/snapshots/latest/")
        try:
            return int(d["total_nodes"])
        except (TypeError, KeyError, ValueError):
            return None
    return _cached("bitnodes", 3600, fetch)


def _fear_greed():
    if is_offline():
        return dict(SYNTHETIC["fng"])

    def fetch():
        d = _http_json("https://api.alternative.me/fng/?limit=1")
        try:
            row = d["data"][0]
            return {"value": int(row["value"]),
                    "classification": str(row.get("value_classification")
                                          or "")}
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    return _cached("fng", 3600, fetch)


# --------------------------------------------------------------------------- #
# Colours
# --------------------------------------------------------------------------- #

def fee_color(fee_sat_vb):
    """Fee pressure: cheap green, elevated amber, painful red."""
    if fee_sat_vb is None:
        return LED_NET_BLUE
    if fee_sat_vb <= 10:
        return LED_GREEN
    if fee_sat_vb <= 60:
        return LED_AMBER
    return LED_RED


def _fng_color(value):
    """Market mood: fear red, neutral amber, greed green."""
    if value < 25:
        return LED_RED
    if value < 55:
        return LED_AMBER
    return LED_GREEN


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #

def _unit_frame(fid, value, pair, tl, br, color):
    """Unit on the face, the device-native way: `pair` renders "/<unit>" in
    the leading cell (exactly how the price frame shows /BTC/AUD) with the
    number right-justified in the remaining 6 slots. If the number needs more
    than 6 digits the pair would make the firmware truncate them - fall back
    to the full number with the unit as the small br caption instead."""
    if len(str(value)) <= SLOTS - 1:
        return _frame(fid, show_number_path(value, pair=pair, tl=tl),
                      number=value, pair=pair, color=color)
    return _frame(fid, show_number_path(value, tl=tl, br=br),
                  number=value, color=color)


def _network_frames(options, wanted=None):
    stats = {s for s in options.get("stats", []) if s in NETWORK_STATS}

    def want(fid):
        if wanted is None:
            return True
        if fid not in wanted:
            return False
        # legacy stats keep their original picker as a second gate
        return fid not in NETWORK_STATS or fid in stats

    frames = []
    add = frames.append

    # ---- chain tip ----
    if want("block_height") or want("halving") or want("halving_days"):
        net = network_snapshot()
        h = net.get("height")
        if h is not None:
            if want("block_height"):
                add(_frame("block_height",
                           show_number_path(h, tl="block height"),
                           number=h, color=LED_NET_BLUE))
            left = HALVING_INTERVAL - (h % HALVING_INTERVAL)
            if want("halving"):
                add(_frame("halving",
                           show_number_path(left, tl="halving",
                                            br="blocks to go"),
                           number=left, color=LED_NET_BLUE))
            if want("halving_days"):
                days = int(left * 10 / 60 / 24)
                add(_frame("halving_days",
                           show_number_path(days, tl="halving",
                                            br="days approx"),
                           number=days, color=LED_NET_BLUE))
    if want("block_age"):
        ts = _tip_timestamp()
        if ts:
            age = max(0, int((time.time() - ts) / 60))
            if age <= 9_999_999:
                add(_frame("block_age",
                           show_number_path(age, tl="block age",
                                            br="minutes"),
                           number=age, color=LED_NET_BLUE))

    # ---- mining ----
    if want("hashrate") or want("difficulty_abs"):
        hr = _hashrate()
        if isinstance(hr, dict):
            try:
                ehs = int(round(float(hr["currentHashrate"]) / 1e18))
                if want("hashrate"):
                    add(_unit_frame("hashrate", ehs, "EH/S", "hashrate",
                                    "EH/s", LED_NET_BLUE))
            except (TypeError, KeyError, ValueError):
                pass
            try:
                dt = float(hr["currentDifficulty"]) / 1e12
                if want("difficulty_abs") and dt < 10_000:
                    val = f"{dt:.1f}"
                    add(_frame("difficulty_abs",
                               show_number_path(val, tl="difficulty",
                                                br="trillions"),
                               number=val, color=LED_NET_BLUE))
            except (TypeError, KeyError, ValueError):
                pass
    if want("difficulty") or want("retarget_blocks"):
        diff = _diff_adj()
        if isinstance(diff, dict):
            dc = diff.get("difficultyChange")
            if want("difficulty") and dc is not None:
                val = f"{float(dc):.1f}"  # signed one-decimal %
                add(_frame("difficulty",
                           show_number_path(val, tl="difficulty",
                                            br="% next epoch"),
                           number=val, color=LED_NET_BLUE))
            rb = diff.get("remainingBlocks")
            if want("retarget_blocks") and rb is not None:
                add(_frame("retarget_blocks",
                           show_number_path(int(rb), tl="retarget",
                                            br="blocks to go"),
                           number=int(rb), color=LED_NET_BLUE))
    if want("fees_day"):
        total = _reward_total_fee()
        if total:
            val = f"{total / 1e8:.2f}"
            add(_unit_frame("fees_day", val, "BTC/DAY", "miner fees",
                            "BTC per day", LED_NET_BLUE))
    if want("pool_dominance"):
        pools = _pools()
        if pools:
            try:
                total = sum(int(p.get("blockCount") or 0) for p in pools)
                top = pools[0]
                pct = int(round(int(top["blockCount"]) / total * 100)) \
                    if total else None
                if pct is not None:
                    name = _fit_line(str(top.get("name") or "top pool")
                                     .upper())
                    add(_frame("pool_dominance",
                               show_number_path(pct, tl=name,
                                                br="% blocks 1w"),
                               number=pct, color=LED_NET_BLUE))
            except (TypeError, KeyError, ValueError):
                pass

    # ---- mempool + fees ----
    if want("mempool") or want("mempool_vsize"):
        mp = _mempool()
        if isinstance(mp, dict):
            n = mp.get("count")
            if want("mempool") and n is not None and n <= 9_999_999:
                add(_frame("mempool",
                           show_number_path(int(n), tl="mempool",
                                            br="unconfirmed tx"),
                           number=int(n), color=LED_NET_BLUE))
            vs = mp.get("vsize")
            if want("mempool_vsize") and vs is not None:
                vmb = int(round(float(vs) / 1e6))
                if vmb <= 9_999_999:
                    add(_frame("mempool_vsize",
                               show_number_path(vmb, tl="mempool",
                                                br="vMB backlog"),
                               number=vmb, color=LED_NET_BLUE))
    fee_frames = [("fees", "fastestFee", "fastest fee", "sat/vB"),
                  ("fee_half_hour", "halfHourFee", "30 min fee", "sat/vB"),
                  ("fee_hour", "hourFee", "1 hour fee", "sat/vB"),
                  ("fee_economy", "economyFee", "economy fee", "sat/vB")]
    if any(want(fid) for fid, *_ in fee_frames):
        fees = _fees()
        if isinstance(fees, dict):
            for fid, field, tl, br in fee_frames:
                if want(fid) and fees.get(field) is not None:
                    try:
                        fee = int(fees[field])
                    except (TypeError, ValueError):
                        continue
                    add(_unit_frame(fid, fee, "SAT/VB", tl, br,
                                    fee_color(fee)))

    # ---- lightning ----
    if want("ln_nodes") or want("ln_channels") or want("ln_capacity"):
        ln = _ln_stats()
        if isinstance(ln, dict):
            specs = [("ln_nodes", "node_count", 1, "lightning", "nodes"),
                     ("ln_channels", "channel_count", 1, "lightning",
                      "channels"),
                     ("ln_capacity", "total_capacity", 1e8, "LN capacity",
                      "BTC")]
            for fid, field, div, tl, br in specs:
                if want(fid) and ln.get(field) is not None:
                    try:
                        val = int(round(float(ln[field]) / div))
                    except (TypeError, ValueError):
                        continue
                    if val <= 9_999_999:
                        add(_frame(fid, show_number_path(val, tl=tl, br=br),
                                   number=val, color=LED_NET_BLUE))

    # ---- supply + activity (blockchain.info /q) ----
    if want("pct_mined") or want("btc_left"):
        totalbc = _totalbc()
        if totalbc:
            mined_btc = totalbc / 1e8
            if want("pct_mined"):
                val = f"{mined_btc / 21e6 * 100:.2f}"
                add(_frame("pct_mined",
                           show_number_path(val, tl="btc mined",
                                            br="% of 21M"),
                           number=val, color=LED_NET_BLUE))
            if want("btc_left"):
                left = int(21e6 - mined_btc)
                add(_frame("btc_left",
                           show_number_path(left, tl="left to mine",
                                            br="BTC"),
                           number=left, color=LED_NET_BLUE))
    if want("txns_24h"):
        n = _txns_24h()
        if n and n <= 9_999_999:
            add(_frame("txns_24h",
                       show_number_path(n, tl="transactions", br="last 24h"),
                       number=n, color=LED_NET_BLUE))
    if want("block_time_avg"):
        s = _block_interval()
        if s and s <= 9_999_999:
            add(_frame("block_time_avg",
                       show_number_path(s, tl="avg block", br="seconds"),
                       number=s, color=LED_NET_BLUE))
    if want("nodes_reachable"):
        n = _bitnodes()
        if n and n <= 9_999_999:
            add(_frame("nodes_reachable",
                       show_number_path(n, tl="btc nodes", br="reachable"),
                       number=n, color=LED_NET_BLUE))

    # ---- market mood ----
    if want("fear_greed"):
        fng = _fear_greed()
        if isinstance(fng, dict) and fng.get("value") is not None:
            word = _fit_line(str(fng.get("classification") or "").upper()) \
                or "MOOD"
            add(_frame("fear_greed",
                       show_number_path(fng["value"], tl="fear & greed",
                                        br=word),
                       number=fng["value"], color=_fng_color(fng["value"])))
    return frames


STAT_LABELS = {
    "block_height": "Block height",
    "fees": "Fastest fee (sat/vB)",
    "halving": "Halving countdown",
    "difficulty": "Difficulty change (%)",
    "mempool": "Mempool backlog (tx count)",
}

register_source(
    "network",
    label="Bitcoin network",
    category="network",
    description="Live network stats from mempool.space, blockchain.info, "
                "Bitnodes and alternative.me (all keyless, no account).",
    frames=[
        ("block_height", "Block height"),
        ("block_age", "Block age (minutes)"),
        ("hashrate", "Hashrate (EH/s)"),
        ("difficulty_abs", "Difficulty (trillions)"),
        ("difficulty", "Difficulty change (%)"),
        ("retarget_blocks", "Retarget in (blocks)"),
        ("mempool", "Mempool (tx count)"),
        ("mempool_vsize", "Mempool (vMB)"),
        ("fees", "Fastest fee"),
        ("fee_half_hour", "30-minute fee"),
        ("fee_hour", "1-hour fee"),
        ("fee_economy", "Economy fee"),
        ("fees_day", "Miner fees (BTC/day)"),
        ("pool_dominance", "Top pool dominance"),
        ("ln_nodes", "Lightning nodes"),
        ("ln_channels", "Lightning channels"),
        ("ln_capacity", "Lightning capacity (BTC)"),
        ("halving", "Halving (blocks)"),
        ("halving_days", "Halving (days)"),
        ("pct_mined", "% of 21M mined"),
        ("btc_left", "BTC left to mine"),
        ("txns_24h", "Transactions (24h)"),
        ("block_time_avg", "Avg block time (s)"),
        ("nodes_reachable", "Reachable nodes"),
        ("fear_greed", "Fear & Greed index"),
    ],
    builder=_network_frames,
    options_schema={
        "stats": {"type": "multi", "label": "Stats to show",
                  "choices": [{"id": s, "label": STAT_LABELS[s]}
                              for s in NETWORK_STATS],
                  "default": ["block_height", "fees", "halving"]},
    },
)
