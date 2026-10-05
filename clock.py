"""BLOCKCLOCK HTTP client + the 7-slot preview model.

Adapted from a proven predecessor feeder. The discipline encoded here was
verified against real hardware and must not be "simplified":

  * one display write per ~65s (HTTP 429 above that). A 429 is NOT success:
    wait a full window and retry.
  * after each accepted write the device goes dark for 30-60s while it repaints;
    a no-reply/connection-reset right after a write means "accepted".
  * 7 character slots; a `pair` eats slot 0; `sym` is silently dropped by the
    firmware if no slot is free.
  * LEDs are global only: /api/lights sets all four to ONE RRGGBBWW colour or a
    named pattern (off/white/flash/yellow_stars/yellow_1).
  * /api/status is unmetered and safe to poll; it is the discovery fingerprint
    ("is_micro", "version") and the verification oracle.
  * an optional System Password makes the device demand HTTP Digest auth with
    an EMPTY username. Every request to the clock goes through `open_clock`,
    the single place that applies it.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException

log = logging.getLogger("blockclock")

SLOTS = 7

PASSWORD_ENV = "BLOCKCLOCK_PASSWORD"   # overrides clock.password in config

# LED palette. Deliberately muted; this sits in a room, not a rave.
LED_BTC_ORANGE = "F7931A00"   # Bitcoin orange - price frames
LED_NET_BLUE = "1050C000"     # calm blue - network frames
LED_RED = "C0180800"          # fees painful / very hot
LED_AMBER = "E07E0000"        # fees elevated / hot
LED_GREEN = "18B31800"        # fees cheap / mild weather
LED_ICE_BLUE = "3060E000"     # freezing weather
LED_WARM_WHITE = "00000060"   # W channel only - neutral frames


# --------------------------------------------------------------------------- #
# Auth: the one path every clock request takes
# --------------------------------------------------------------------------- #

class ClockAuthError(Exception):
    """The clock answered 401: it needs a password, or rejected ours. Never
    "offline", never retried in a loop - the caller must surface it."""


def clock_password(clock_cfg):
    """The configured System Password: env var wins over config.json's
    clock.password. Empty string means no auth."""
    env = os.environ.get(PASSWORD_ENV)
    if env:
        return env
    return str((clock_cfg or {}).get("password") or "")


class _DigestOnce(urllib.request.HTTPDigestAuthHandler):
    """Answer a challenge ONCE. urllib would re-send a wrong password up to
    five more times; a second 401 means it is wrong, so stop there."""

    def retry_http_digest_auth(self, req, auth):
        if self.retried > 1:
            return None
        return super().retry_http_digest_auth(req, auth)


def build_opener(base, password=""):
    handlers = []
    if password:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        # digest with an EMPTY username, per the vendor spec
        mgr.add_password(None, base, "", password)
        handlers.append(_DigestOnce(mgr))
    return urllib.request.build_opener(*handlers)


def open_clock(base, path, password="", timeout=40, opener=None):
    """GET base+path, answering a Digest challenge when a password is set.
    A 401 becomes ClockAuthError (the message never contains the password);
    every other outcome is left to the caller exactly as urllib raises it."""
    opener = opener or build_opener(base, password)
    req = urllib.request.Request(base + path,
                                 headers={"User-Agent": "blockclock-connect/1"})
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        e.close()
        if password:
            raise ClockAuthError("The clock rejected the password. Check it "
                                 "matches the clock's System Password.") from None
        raise ClockAuthError("The clock requires a password. Enter its "
                             "System Password.") from None


# --------------------------------------------------------------------------- #
# Display-command path builders (pure functions; no device I/O)
# --------------------------------------------------------------------------- #

def _q(**params):
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return ("?" + urllib.parse.urlencode(clean)) if clean else ""


def show_number_path(number, sym=None, pair=None, tl=None, br=None, omit_line=None):
    path = "/api/show/number/" + urllib.parse.quote(str(number))
    return path + _q(sym=sym, pair=pair, tl=tl, br=br,
                     omit_line=(1 if omit_line else None))


def show_text_path(text, tl=None, br=None):
    path = "/api/show/text/" + urllib.parse.quote(str(text))
    return path + _q(tl=tl, br=br)


def lights_path(spec):
    return "/api/lights/" + urllib.parse.quote(str(spec))


def pick_path(tag):
    return "/api/pick/" + urllib.parse.quote(str(tag))


def pause_path():
    return "/api/action/pause"


def update_rate_path(minutes):
    # re-enables auto-updates (the effective "un-pause") and sets the interval
    return f"/api/action/update?rate={int(minutes)}"


# --------------------------------------------------------------------------- #
# 7-slot preview (mirrors observed firmware truncation / sym-drop behaviour)
# --------------------------------------------------------------------------- #

def preview_slots(number=None, text=None, sym=None, pair=None):
    """Approximate what the device renders into its 7 slots. Firmware rules:
    pair eats slot 0, digits fill from the right, sym only appears if a slot
    is left over."""
    cells = [" "] * SLOTS
    idx = list(range(SLOTS))
    if pair:
        cells[0] = f"/{pair}"
        idx = list(range(1, SLOTS))
    if text is not None:
        s = str(text).upper()[:len(idx)]
        for i, ch in zip(idx, s.rjust(len(idx))):
            cells[i] = ch
        return cells
    digits = str(number)
    want_sym = bool(sym)
    if want_sym and len(digits) < len(idx):
        # sym takes the leftmost remaining slot
        cells[idx[0]] = sym
        idx = idx[1:]
    placed = digits[-len(idx):] if len(digits) > len(idx) else digits
    for i, ch in zip(idx[-len(placed):], placed):
        cells[i] = ch
    return cells


# --------------------------------------------------------------------------- #
# Clock client
# --------------------------------------------------------------------------- #

class ClockClient:
    """Talks to the BLOCKCLOCK push API with rate + repaint-stall discipline."""

    def __init__(self, host, password="", write_interval_s=65):
        self.host = host
        self.base = f"http://{host}"
        self.password = password or ""
        self.write_interval_s = write_interval_s
        self._last_write = 0.0
        self._opener = self._build_opener()
        self.stop_event = None  # optional threading.Event; set -> abort waits

    def _build_opener(self):
        return build_opener(self.base, self.password)

    def set_password(self, password):
        self.password = password or ""
        self._opener = self._build_opener()

    def _get(self, path, timeout=40):
        return open_clock(self.base, path, self.password, timeout,
                          opener=self._opener)

    def status(self, timeout=15):
        """/api/status is unmetered; the verification oracle. Dict or None."""
        try:
            with self._get("/api/status", timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except ClockAuthError:
            raise
        except Exception as e:
            log.info("status(%s) failed: %r", self.host, e)
            return None

    def _stopping(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def seconds_until_ready(self):
        return max(0.0, self.write_interval_s - (time.time() - self._last_write))

    def _rate_wait(self):
        wait = self.seconds_until_ready()
        if wait > 0:
            log.info("rate-limit: sleeping %.0fs before next write", wait)
            # interruptible: a stop signal wakes us immediately
            if self.stop_event is not None:
                self.stop_event.wait(wait)
            else:
                time.sleep(wait)

    def push(self, path, respect_rate=True, _tries=2, timeout=40):
        """Push a display command. Returns True if the frame was (very likely)
        taken. Outcomes, distinguished carefully:

          * clean 2xx reply         -> accepted (True)
          * no reply / conn reset / -> device went dark to repaint (30-60s);
            garbage instead of a       EXPECTED right after a write -> True
            status line
          * HTTP 429 (rate limited) -> NOT accepted; wait a full window, retry.
                                       Out of retries -> give up (False)
          * other HTTP error        -> device answered with an error -> False
          * HTTP 401                -> ClockAuthError (needs/rejected password);
                                       the caller surfaces it, no retry here
        """
        if respect_rate:
            self._rate_wait()
        # if a stop arrived while (or before) we waited, don't emit a stray frame
        if respect_rate and self._stopping():
            return False
        try:
            with self._get(path, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                self._last_write = time.time()
                log.info("push %s -> %s", path, body.strip()[:80])
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if _tries > 1:
                    log.info("push %s -> 429 rate-limited; waiting %ss and retrying",
                             path, self.write_interval_s)
                    self._last_write = time.time()  # reset the window before retry
                    if self.stop_event is not None:
                        if self.stop_event.wait(self.write_interval_s):
                            return False
                    else:
                        time.sleep(self.write_interval_s)
                    return self.push(path, respect_rate=False, _tries=_tries - 1,
                                     timeout=timeout)
                log.info("push %s -> 429 and out of retries; giving up this frame", path)
                return False
            self._last_write = time.time()
            log.info("push %s -> HTTP %s; not accepted", path, e.code)
            return False
        except (urllib.error.URLError, HTTPException, TimeoutError, OSError) as e:
            # no reply / reset / an unparseable half-answer right after a write
            # == it took the frame and went dark (HTTPException covers
            # BadStatusLine and RemoteDisconnected)
            self._last_write = time.time()
            log.info("push %s -> no clean reply (%r); treating as accepted "
                     "(repaint stall)", path, e)
            return True
