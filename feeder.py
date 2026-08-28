"""The background display feeder.

A single writer thread that owns the connected BLOCKCLOCK:

  * drives the rotation of enabled frames 24/7, one display write per rate
    window (>=65s), tinting the backlight to match each frame;
  * pauses the device's own screen rotation while driving and RESTORES it
    (lights off, auto-updates back on, original screen picked) on disconnect
    and on shutdown - fast and best-effort, exactly like the proven feeder;
  * hot-reloads whenever the config generation changes (frames are rebuilt
    from live config every cycle, so a save simply takes effect on the next
    write);
  * honours the per-frame settings in config["frames"]: `dwell` repeats a
    frame across N write-windows before advancing, and `window`
    ({from_hour,to_hour}, local, wrap-around ok) skips a frame outside its
    hours - if EVERY rotation frame is out of window, the clock is handed
    back to its own screens until a window opens;
  * services one-off "test this frame now" requests from the UI, respecting
    the same rate window; ANY enabled frame id may be tested, in rotation
    or not;
  * when the Shopify (Merchant) source is enabled, detects sale/milestone
    EVENTS and lets them preempt the rotation: a persistent pending queue in
    store.state is drained one event per write window, then rotation resumes
    (ported from a proven predecessor Scheduler). A Shopify failure just
    means "no events this cycle" - it never crashes or stalls the rotation.
"""

import logging
import threading
import time
import urllib.error
from http.client import HTTPException

from clock import ClockClient, lights_path, pause_path, pick_path, \
    preview_slots, update_rate_path
import sources
from sources import build_frames
from store import MIN_WRITE_INTERVAL_S

log = logging.getLogger("feeder")

RESTORE_RATE_MIN = 5          # auto-update interval restored on shutdown
FALLBACK_TAG = "cm.markets.price"
IDLE_POLL_S = 5

MILESTONES = [1, 5, 10, 25, 50, 100]   # order-count celebrations per day
MAX_PENDING_EVENTS = 6        # cap so a burst can't monopolise the screen

# Transport failures are ROUTINE, not exceptional: the device stops
# answering (or answers garbage) for 30-60s while it repaints. One bad
# cycle costs one frame - it must never end the feeder.
TRANSIENT_ERRORS = (HTTPException, urllib.error.URLError, OSError)
FAIL_ALERT_AFTER = 5          # consecutive bad cycles before an ERROR
FAIL_BACKOFF_MAX_S = 30       # cap on the post-failure pause


class Feeder(threading.Thread):
    def __init__(self, store):
        super().__init__(name="feeder", daemon=True)
        self.store = store
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self.client = None            # ClockClient for the connected ip
        self.driving = False
        self.last_frame = None        # {name,label,slots,color,at,accepted}
        self.rot_index = 0
        self._dwell_name = None       # frame currently being dwelt on
        self._dwell_left = 0          # write-windows it still holds
        self._test_request = None     # frame name queued by /api/test
        self._custom_request = None   # full frame dict queued by /agent/show
        self._last_color = None
        self.original_tag = None
        self.push_fails = 0           # consecutive failed cycles

    # ------------------------------------------------------------------ API #

    def notify_config_changed(self):
        self.wake.set()

    def request_test(self, frame_name):
        """Queue one frame to be pushed in the next rate window. ANY frame an
        enabled source can build is accepted - it does not have to be in the
        rotation. Returns (ok, message)."""
        cfg = self.store.config
        if not cfg.get("clock"):
            return False, "No clock connected yet"
        frames = build_frames(cfg, all_frames=True)
        if not frames:
            return False, "No feeds are enabled (or their sources are down)"
        names = [f["name"] for f in frames]
        chosen = frame_name if frame_name in names else names[0]
        with self._lock:
            self._test_request = chosen
        self.wake.set()
        eta = int(self.client.seconds_until_ready()) if self.client else 0
        return True, (f"Queued '{chosen}' - it will appear on the clock "
                      f"in about {max(eta, 2)}s (rate limit is one write "
                      f"per {MIN_WRITE_INTERVAL_S}s)")

    def request_custom(self, frame):
        """Queue ONE agent-built frame (a full frame dict: name/label/source/
        path/slotargs) to be pushed in the next rate window. It preempts the
        rotation for exactly one write, exactly like a test push - it NEVER
        bypasses the one-write-per-window rate discipline. A newer request
        replaces a queued one. Returns (ok, message)."""
        cfg = self.store.config
        if not cfg.get("clock"):
            return False, "No clock connected yet"
        with self._lock:
            self._custom_request = frame
        self.wake.set()
        eta = int(self.client.seconds_until_ready()) if self.client else 0
        return True, (f"Queued - it will appear on the clock in about "
                      f"{max(eta, 2)}s (rate limit is one write per "
                      f"{MIN_WRITE_INTERVAL_S}s)")

    def next_write_in_s(self):
        if self.client and self.driving:
            return int(self.client.seconds_until_ready())
        return None

    def shutdown(self):
        """Called from the signal handler: stop the loop and hand the clock
        back, fast."""
        self.stop_event.set()
        self.wake.set()
        if self.driving:
            self._restore()

    # ------------------------------------------------------- internals #

    def _new_client(self, ip, interval):
        client = ClockClient(ip, write_interval_s=interval)
        client.stop_event = self.stop_event
        # survive restarts without an instant 429: honour the persisted
        # last-write timestamp if it is recent
        last = self.store.state.get("last_write_ts")
        if isinstance(last, (int, float)) and 0 < time.time() - last < interval:
            client._last_write = last
        return client

    def _start_driving(self):
        st = self.client.status()
        if st:
            self.original_tag = (st.get("rendered") or {}).get("tag")
            self.store.update_state(original_tag=self.original_tag)
            log.info("clock reachable; original tag %r, fw %s",
                     self.original_tag, st.get("version"))
        else:
            self.original_tag = self.store.state.get("original_tag")
            log.warning("clock not answering /api/status; driving anyway")
        # pause the device's own rotation so we own the screen
        self.client.push(pause_path(), respect_rate=False, _tries=1, timeout=10)
        self.driving = True
        self._last_color = None

    def _restore(self):
        """Undo the startup pause, FAST and best-effort (must not stall a
        container stop). Re-enabling auto-updates is what actually recovers
        the clock: even if the final pick 429s, the device resumes its own
        rotation within a few minutes on its own."""
        client = self.client
        if not client:
            return
        tag = self.original_tag or self.store.state.get("original_tag") \
            or FALLBACK_TAG
        for path in (lights_path("off"),
                     update_rate_path(RESTORE_RATE_MIN),
                     pick_path(tag)):
            try:
                client.push(path, respect_rate=False, _tries=1, timeout=5)
            except Exception:
                pass
        self.driving = False
        self._last_color = None
        log.info("restored clock %s: auto-updates on, aiming for %r",
                 client.host, tag)

    def _set_lights(self, color):
        """Best-effort backlight change (fail-fast). The LED write must never
        consume the display's rate window, so the write timestamp is restored
        around it."""
        if not color or color == self._last_color:
            return
        saved = self.client._last_write
        try:
            if self.client.push(lights_path(color), respect_rate=False,
                                _tries=1, timeout=10):
                self._last_color = color
        except Exception:
            pass
        self.client._last_write = saved

    def _record_frame(self, frame, accepted):
        slotargs = dict(frame["slotargs"])
        color = slotargs.pop("color", None)
        self.last_frame = {
            "name": frame["name"],
            "label": frame["label"],
            "slots": preview_slots(**slotargs),
            "color": color,
            "at": int(time.time()),
            "accepted": bool(accepted),
        }
        self.store.update_state(last_frame=self.last_frame,
                                last_write_ts=self.client._last_write)

    def _pop_test(self):
        with self._lock:
            t = self._test_request
            self._test_request = None
            return t

    def _pop_custom(self):
        with self._lock:
            f = self._custom_request
            self._custom_request = None
            return f

    # -------------------------------------- per-frame dwell + window #

    @staticmethod
    def _window_ok(setting, hour):
        """True when a frame's {from_hour,to_hour} window (if any) covers the
        given local hour. from==to (or a missing bound) means always;
        from>to wraps around midnight (e.g. 22 -> 6)."""
        win = (setting or {}).get("window")
        if not isinstance(win, dict):
            return True
        f, t = win.get("from_hour"), win.get("to_hour")
        if f is None or t is None or f == t:
            return True
        if f < t:
            return f <= hour < t
        return hour >= f or hour < t

    def _pick_rotation_frame(self, frames, settings, hour=None):
        """Pick the frame for this write-window from the (already window-
        filtered) rotation frames, honouring `dwell`: a frame with dwell=N
        holds the screen for N consecutive windows before the rotation
        advances. Events/tests preempting in between don't reset the hold."""
        if not frames:
            return None
        if self._dwell_left > 0 and self._dwell_name:
            held = next((f for f in frames if f["name"] == self._dwell_name),
                        None)
            if held is not None:
                self._dwell_left -= 1
                return held
            self._dwell_left = 0   # the held frame vanished; move on
        frame = frames[self.rot_index % len(frames)]
        self.rot_index += 1
        try:
            dwell = int((settings.get(frame["name"]) or {}).get("dwell", 1))
        except (TypeError, ValueError):
            dwell = 1
        self._dwell_name = frame["name"]
        self._dwell_left = max(0, dwell - 1)
        return frame

    # ------------------------------------------- shopify events #

    def _shopify_options(self, cfg):
        """The shopify options dict IF events should run (source enabled and
        credentialed, or offline synthetic), else None."""
        src = (cfg.get("sources") or {}).get("shopify") or {}
        if not src.get("enabled"):
            return None
        opts = src.get("options") or {}
        if not sources.is_offline() \
                and not (opts.get("shop_domain") and opts.get("token")):
            return None
        return opts

    def _rollover_if_needed(self, opts):
        """Reset the daily counters at local (shop tz) midnight; yesterday's
        revenue is archived into the persistent all-time daily record."""
        today = sources.shopify_day_key(opts.get("tz_offset_hours", 10))
        st = self.store.state
        if st.get("shopify_day") and st.get("shopify_day") != today:
            record = max(float(st.get("shopify_record_revenue") or 0),
                         float(st.get("shopify_last_revenue") or 0))
            self.store.update_state(
                shopify_day=today, shopify_seen_ids=[],
                shopify_milestones_hit=[], shopify_record_seen=False,
                shopify_record_revenue=record, shopify_last_revenue=0.0)
            log.info("shopify daily rollover -> %s (all-time daily record "
                     "%.0f)", today, record)

    def _detect_shopify_events(self, snap, opts):
        """Append new sale/milestone events to the persistent pending queue
        in store.state. COLD START (no shopify_day recorded yet) SEEDS the
        already-passed order ids / milestones / record silently, so restarting
        mid-day never fires retroactive alarms - exactly like the proven
        feeder. Multiple new orders in one detection coalesce into one frame."""
        st = self.store.state
        pending = list(st.get("pending_events") or [])
        seen = set(st.get("shopify_seen_ids") or [])
        current = set(snap.get("order_ids") or [])
        new_ids = current - seen
        cold_start = not st.get("shopify_day")
        ccy = opts.get("currency", "AUD")

        if new_ids and not cold_start:
            pending.append(sources.build_sale_alert(
                snap.get("latest_total"), snap.get("latest_city"),
                currency=ccy, count=len(new_ids)))

        hit = set(st.get("shopify_milestones_hit") or [])
        for m in MILESTONES:
            if snap["order_count"] >= m and m not in hit:
                if not cold_start:
                    pending.append(sources.build_milestone(
                        "first" if m == 1 else "count", m))
                hit.add(m)

        record = float(st.get("shopify_record_revenue") or 0)
        record_seen = bool(st.get("shopify_record_seen"))
        if record > 0 and snap["revenue"] > record and not record_seen:
            if not cold_start:
                pending.append(sources.build_milestone(
                    "record", int(snap["revenue"])))
            record_seen = True

        if len(pending) > MAX_PENDING_EVENTS:
            log.info("event backlog capped; dropped %d oldest",
                     len(pending) - MAX_PENDING_EVENTS)
            pending = pending[-MAX_PENDING_EVENTS:]

        self.store.update_state(
            pending_events=pending,
            shopify_seen_ids=sorted(current),
            shopify_milestones_hit=sorted(hit),
            shopify_record_seen=record_seen,
            shopify_last_revenue=snap["revenue"],
            shopify_day=st.get("shopify_day")
            or sources.shopify_day_key(opts.get("tz_offset_hours", 10)))

    def _next_shopify_event(self, cfg):
        """Detect, then pop AT MOST ONE pending event frame (or None). Any
        Shopify failure just means no events this cycle - never raises, never
        stalls the rotation. NB: nothing here may ever log the token."""
        opts = self._shopify_options(cfg)
        if opts is None:
            return None
        try:
            self._rollover_if_needed(opts)
            snap = sources.shopify_snapshot(opts.get("shop_domain"),
                                            opts.get("token"),
                                            opts.get("tz_offset_hours", 10))
            if snap:
                self._detect_shopify_events(snap, opts)
        except Exception as e:
            log.warning("shopify event detection failed: %s",
                        type(e).__name__)
        pending = list(self.store.state.get("pending_events") or [])
        if not pending:
            return None
        frame = pending.pop(0)
        self.store.update_state(pending_events=pending)
        return frame

    def _celebrate_lights(self, frame, cfg):
        """Backlight celebration right before an event frame: 'flash' for
        sales (honouring flash_on_sale), 'yellow_1' for milestones. Best-
        effort and fail-fast like _set_lights; never consumes the display's
        rate window. The rotation re-asserts its own tint on the next frame."""
        name = str(frame.get("name") or "")
        opts = ((cfg.get("sources") or {}).get("shopify")
                or {}).get("options") or {}
        self._last_color = None
        if name.startswith("sale") and not opts.get("flash_on_sale", True):
            return
        spec = (frame.get("slotargs") or {}).get("color") \
            or ("flash" if name.startswith("sale") else "yellow_1")
        saved = self.client._last_write
        try:
            self.client.push(lights_path(spec), respect_rate=False,
                             _tries=1, timeout=10)
        except Exception:
            pass
        self.client._last_write = saved

    # ------------------------------------------------------------- loop #

    def run(self):
        log.info("feeder started")
        try:
            while not self.stop_event.is_set():
                cfg = self.store.config
                clock_cfg = cfg.get("clock")

                # -- not connected: make sure the clock is handed back, idle
                if not clock_cfg:
                    if self.driving:
                        self._restore()
                    self.client = None
                    self.wake.clear()
                    self.wake.wait(IDLE_POLL_S)
                    continue

                interval = max(int(cfg.get("write_interval_s",
                                           MIN_WRITE_INTERVAL_S)),
                               MIN_WRITE_INTERVAL_S)

                # -- (re)connect the client if the target ip changed
                if self.client is None or self.client.host != clock_cfg["ip"]:
                    if self.driving:
                        self._restore()   # hand the OLD clock back first
                    self.client = self._new_client(clock_cfg["ip"], interval)
                    self.rot_index = 0
                self.client.write_interval_s = interval

                # -- build what we can show right now (the rotation view)
                try:
                    frames = build_frames(cfg)
                except Exception as e:
                    log.warning("build_frames failed: %r", e)
                    frames = []

                # a test may name ANY enabled frame, in rotation or not
                test_name = self._pop_test()
                test_frame = None
                if test_name:
                    try:
                        allf = build_frames(cfg, all_frames=True)
                    except Exception:
                        allf = frames
                    test_frame = next(
                        (f for f in allf if f["name"] == test_name),
                        frames[0] if frames else (allf[0] if allf else None))

                # an agent-built one-off frame rides the same one-shot slot
                # (it wins over a plain test queued in the same window)
                custom_frame = self._pop_custom()
                if custom_frame is not None:
                    test_frame = custom_frame

                # per-frame windows: out-of-hours frames sit this cycle out
                settings = cfg.get("frames") or {}
                hour = time.localtime().tm_hour
                showable = [f for f in frames
                            if self._window_ok(settings.get(f["name"]), hour)]

                if not showable and not test_frame:
                    # nothing to show (no frames, or all outside their
                    # windows) -> give the screen back, wait
                    if self.driving:
                        self._restore()
                    self.wake.clear()
                    self.wake.wait(IDLE_POLL_S * 2)
                    continue

                # everything that talks to the device happens in here:
                # a transport failure costs THIS cycle, nothing more
                try:
                    if not self.driving:
                        self._start_driving()
                        if self.stop_event.is_set():
                            break

                    # a pending sale/milestone event PREEMPTS the rotation:
                    # exactly one event is drained per write window
                    event_frame = None if test_frame \
                        else self._next_shopify_event(cfg)

                    if event_frame:
                        frame = event_frame
                        log.info("event: %s (%d more queued)", frame["name"],
                                 len(self.store.state.get("pending_events")
                                     or []))
                    elif test_frame:
                        frame = test_frame
                        log.info("test frame: %s", frame["name"])
                    else:
                        frame = self._pick_rotation_frame(showable, settings,
                                                          hour)
                        log.info("rotate: %s", frame["name"])

                    # absorb the rate window FIRST so the LED tint lands
                    # seconds (not a whole window) before its frame
                    self.client._rate_wait()
                    if self.stop_event.is_set():
                        break
                    if event_frame:
                        self._celebrate_lights(frame, cfg)
                    else:
                        self._set_lights(frame["slotargs"].get("color"))
                    accepted = self.client.push(frame["path"])  # window spent
                    self._record_frame(frame, accepted)
                except TRANSIENT_ERRORS as e:
                    self.push_fails += 1
                    log.warning("cycle failed (%d in a row): %r",
                                self.push_fails, e)
                    if self.push_fails >= FAIL_ALERT_AFTER:
                        log.error("clock %s has taken no frame for %d "
                                  "cycles", self.client.host,
                                  self.push_fails)
                    # bounded backoff, interruptible: a stop still lands
                    # promptly
                    self.stop_event.wait(min(5 * self.push_fails,
                                             FAIL_BACKOFF_MAX_S))
                    continue
                self.push_fails = 0
        except Exception:
            log.exception("feeder crashed")
        finally:
            if self.driving:
                self._restore()
            log.info("feeder stopped")
