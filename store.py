"""Config + runtime state persistence under DATA_DIR (a mounted volume).

config.json - the user's saved settings (clock, sources, rotation, interval)
state.json  - runtime breadcrumbs (last write timestamp, the clock's original
              screen tag so we can restore it, last frame shown)

All writes are atomic (tmp + os.replace) and lock-guarded; readers get deep
copies so callers can't mutate shared state by accident.
"""

import copy
import json
import logging
import os
import threading

log = logging.getLogger("store")

DEFAULT_CONFIG = {
    "clock": None,  # {"ip", "model", "version"} once connected
    "sources": {
        "price": {"enabled": True, "options": {"exchange": "coinbase",
                                               "currency": "USD",
                                               "compare_a": "kraken",
                                               "compare_b": "coingecko",
                                               "compare_currency": "USD",
                                               "bitaroo_api_key": ""}},
        "network": {"enabled": True,
                    "options": {"stats": ["block_height", "fees", "halving"]}},
        # On-chain analytics (BRK / bitview.space) - keyless daily series
        "brk": {"enabled": False,
                "options": {"series": ["sats_per_usd", "mvrv",
                                       "days_since_ath", "realized_price",
                                       "puell", "nupl", "price_ath",
                                       "supply", "market_cap"]}},
        # The sovereign surface: Esplora frames against your own mempool
        # instance, plus optional Bitcoin Core RPC / LND REST. All secrets
        # (core_pass, lnd_macaroon) live ONLY here on disk - every API
        # response strips them (app.public_config / sources.catalogue).
        "node": {"enabled": False,
                 "options": {"base_url": "https://mempool.space",
                             "core_host": "", "core_port": 8332,
                             "core_user": "", "core_pass": "",
                             "lnd_host": "", "lnd_port": 8080,
                             "lnd_macaroon": ""}},
        "weather": {"enabled": False,
                    "options": {"city": "", "units": "C", "lat": None,
                                "lon": None, "place": None,
                                "sunset_label": "SS",
                                "show_condition": False}},
        "macro": {"enabled": False,
                  "options": {"forex_base": "USD", "forex_quote": "AUD",
                              "metal_currency": "USD",
                              "fred_api_key": ""}},
        "space": {"enabled": False,
                  "options": {"countdown_label": "countdown",
                              "countdown_date": ""}},
        "novelty": {"enabled": False,
                    "options": {"github_repo": "bitcoin/bitcoin"}},
        # Merchant extras, OFF by default. options.token / options.api_key
        # (the user's own credentials) live ONLY here on disk - every API
        # response strips them (app.public_config / sources.catalogue).
        "shopify": {"enabled": False,
                    "options": {"shop_domain": "", "token": "",
                                "currency": "AUD", "daily_goal": 0,
                                "tz_offset_hours": 10,
                                "frames": ["revenue_today", "order_count",
                                           "revenue_sats", "goal"],
                                "flash_on_sale": True}},
        "btcpay": {"enabled": False,
                   "options": {"base_url": "", "store_id": "", "api_key": "",
                               "tz_offset_hours": 10}},
    },
    "rotation": ["btc_price", "block_height", "sats_per_unit", "fees",
                 "halving", "difficulty", "mempool", "weather_temp",
                 "weather_condition", "revenue_today", "order_count",
                 "units_today", "avg_order", "revenue_sats", "last_city",
                 "goal"],
    # per-frame settings, keyed by frame id: {"dwell": N write-windows,
    # "window": {"from_hour", "to_hour"}} - both optional, defaults 1/always
    "frames": {},
    "write_interval_s": 65,
    # OPTIONAL agent/API access, OFF BY DEFAULT. When the user enables it a
    # bearer token is generated; the /agent/* endpoints only answer while
    # enabled AND the caller presents that token. The token lives ONLY here
    # on disk: every config echo redacts it (app.public_config) and the ONE
    # deliberate reveal is GET /api/access-token behind the normal UI path.
    # Turning access off KEEPS the token (re-enabling is stable); only an
    # explicit regenerate replaces it.
    "api_access": {"enabled": False, "token": ""},
}

MIN_WRITE_INTERVAL_S = 65   # >60s keeps clear of the device's 1/min 429 line


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.config_path = os.path.join(data_dir, "config.json")
        self.state_path = os.path.join(data_dir, "state.json")
        self._lock = threading.RLock()
        self._config = copy.deepcopy(DEFAULT_CONFIG)
        self._state = {}
        self.generation = 0  # bumped on every config save (feeder hot-reload)
        self._load()

    # -- disk --------------------------------------------------------------- #

    def _load(self):
        for path, attr in ((self.config_path, "_config"),
                           (self.state_path, "_state")):
            try:
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if attr == "_config":
                        merged = copy.deepcopy(DEFAULT_CONFIG)
                        merged.update(data or {})
                        self._migrate_config(merged)
                        self._config = merged
                    else:
                        self._state = data or {}
            except Exception as e:
                log.warning("could not load %s: %r (using defaults)", path, e)

    @staticmethod
    def _migrate_config(config):
        """Apply small, lossless upgrades to persisted configuration.

        Both former AU price frames are represented by the configurable
        cross-source comparison in 0.5.0. Replace either legacy id in-place,
        deduplicating when a rotation contained both, so an upgrade cannot
        silently empty or shorten the user's rotation.
        """
        legacy = {"au_premium": "price_compare",
                  "au_spread": "price_compare"}
        rotation, seen = [], set()
        for item in config.get("rotation") or []:
            if isinstance(item, dict):
                item = dict(item)
                item["id"] = legacy.get(item.get("id"), item.get("id"))
                fid = item.get("id")
            else:
                item = legacy.get(item, item)
                fid = item
            if fid not in seen:
                rotation.append(item)
                seen.add(fid)
        config["rotation"] = rotation

        settings = config.get("frames")
        if isinstance(settings, dict):
            for old in ("au_premium", "au_spread"):
                if old in settings:
                    settings.setdefault("price_compare", settings[old])
                    settings.pop(old, None)

    def _write(self, path, data):
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            # a broken volume must not take the app down; state just won't persist
            log.warning("could not persist %s: %r", path, e)

    # -- config ------------------------------------------------------------- #

    @property
    def config(self):
        with self._lock:
            return copy.deepcopy(self._config)

    def save_config(self, cfg):
        with self._lock:
            self._config = copy.deepcopy(cfg)
            self.generation += 1
            self._write(self.config_path, self._config)

    # -- state -------------------------------------------------------------- #

    @property
    def state(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def update_state(self, **kv):
        with self._lock:
            self._state.update(kv)
            self._write(self.state_path, self._state)
