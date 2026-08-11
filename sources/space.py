"""Space & time: the ISS (wheretheiss.at), humans in space (open-notify -
plain HTTP, the one non-TLS fetch in the app, clearly flagged), NOAA space
weather (Kp geomagnetic index + F10.7 solar flux), plus two pure-local
frames: day-of-year and a user-set countdown-to-date. The local frames burn
no network call on what time.localtime() already knows.
"""

import time
from datetime import date, datetime

from clock import (LED_AMBER, LED_GREEN, LED_ICE_BLUE, LED_NET_BLUE, LED_RED,
                   LED_WARM_WHITE, show_number_path)
from sources import (_cached, _fit_line, _frame, _http_json, is_offline,
                     register_source)

ISS_API = "https://api.wheretheiss.at/v1/satellites/25544"
# open-notify serves plain HTTP only - allowed knowingly for this one
# harmless statistic (a headcount), noted in the source description.
ASTROS_API = "http://api.open-notify.org/astros.json"
KP_API = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
FLUX_API = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"

SYNTHETIC = {"velocity": 27_608.0, "altitude": 414.2, "humans": 7,
             "kp": 4.33, "flux": 94}


def iss_now():
    """Live ISS speed/altitude (caps ~1 req/s upstream; cached 60s)."""
    if is_offline():
        return {"velocity": SYNTHETIC["velocity"],
                "altitude": SYNTHETIC["altitude"]}

    def fetch():
        try:
            d = _http_json(ISS_API)
            return {"velocity": float(d["velocity"]),
                    "altitude": float(d["altitude"])}
        except Exception:
            return None
    return _cached("iss", 60, fetch)


def humans_in_space():
    if is_offline():
        return SYNTHETIC["humans"]

    def fetch():
        try:
            d = _http_json(ASTROS_API)
            return int(d["number"])
        except Exception:
            return None
    return _cached("astros", 3600, fetch)


def kp_index():
    """Planetary Kp (last reading). NOAA now serves a list of dicts:
    [{"time_tag":..., "Kp":1.67, ...}, ...] (older format was list-of-lists
    with a header row - handle both defensively)."""
    if is_offline():
        return SYNTHETIC["kp"]

    def fetch():
        try:
            d = _http_json(KP_API)
            row = d[-1]
            val = row["Kp"] if isinstance(row, dict) else row[1]
            return float(val)
        except Exception:
            return None
    return _cached("kp", 1800, fetch)


def solar_flux():
    if is_offline():
        return SYNTHETIC["flux"]

    def fetch():
        try:
            d = _http_json(FLUX_API)
            return int(round(float(d[-1]["flux"])))
        except Exception:
            return None
    return _cached("f107", 3600, fetch)


def _kp_color(kp):
    """Kp >= 5 is a geomagnetic storm (aurora watch)."""
    if kp < 4:
        return LED_GREEN
    if kp < 6:
        return LED_AMBER
    return LED_RED


def countdown_days(date_str):
    """Days until a user-set YYYY-MM-DD date, or None (unset/invalid/past)."""
    s = str(date_str or "").strip()
    if not s:
        return None
    try:
        target = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
    days = (target - date.today()).days
    return days if 0 <= days <= 99_999 else None


def _space_frames(options, wanted=None):
    def want(fid):
        return wanted is None or fid in wanted

    frames = []
    add = frames.append

    if want("iss_speed") or want("iss_altitude"):
        iss = iss_now()
        if iss:
            if want("iss_speed"):
                v = int(round(iss["velocity"]))
                add(_frame("iss_speed",
                           show_number_path(v, tl="ISS speed", br="km/h"),
                           number=v, color=LED_ICE_BLUE))
            if want("iss_altitude"):
                a = int(round(iss["altitude"]))
                add(_frame("iss_altitude",
                           show_number_path(a, tl="ISS altitude", br="km up"),
                           number=a, color=LED_ICE_BLUE))
    if want("humans_space"):
        n = humans_in_space()
        if n is not None:
            add(_frame("humans_space",
                       show_number_path(n, tl="humans", br="in space"),
                       number=n, color=LED_WARM_WHITE))
    if want("kp_index"):
        kp = kp_index()
        if kp is not None:
            val = f"{kp:.1f}"
            add(_frame("kp_index",
                       show_number_path(val, tl="Kp geomag", br="storm at 5"),
                       number=val, color=_kp_color(kp)))
    if want("solar_flux"):
        flux = solar_flux()
        if flux is not None:
            add(_frame("solar_flux",
                       show_number_path(flux, tl="solar flux", br="F10.7"),
                       number=flux, color=LED_AMBER))

    # ---- pure-local frames (work even fully offline) ----
    if want("day_of_year"):
        lt = time.localtime()
        total = 366 if date(lt.tm_year, 12, 31).timetuple().tm_yday == 366 \
            else 365
        add(_frame("day_of_year",
                   show_number_path(lt.tm_yday, tl="day", br=f"of {total}"),
                   number=lt.tm_yday, color=LED_NET_BLUE))
    if want("countdown"):
        days = countdown_days(options.get("countdown_date"))
        if days is not None:
            label = _fit_line(options.get("countdown_label") or "countdown")
            add(_frame("countdown",
                       show_number_path(days, tl=label, br="days to go"),
                       number=days, color=LED_ICE_BLUE))
    return frames


def _validate(src_out, fail):
    opts = src_out["options"]
    s = str(opts.get("countdown_date") or "").strip()
    if s:
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            fail("space.countdown_date must be YYYY-MM-DD")
            s = ""
    opts["countdown_date"] = s


register_source(
    "space",
    label="Space & time",
    category="space",
    description="The ISS live, how many humans are in space right now "
                "(open-notify, plain-HTTP), NOAA aurora/solar activity, "
                "day-of-year, and a countdown to any date you set.",
    frames=[
        ("iss_speed", "ISS speed (km/h)"),
        ("iss_altitude", "ISS altitude (km)"),
        ("humans_space", "Humans in space"),
        ("kp_index", "Kp geomagnetic index"),
        ("solar_flux", "Solar flux (F10.7)"),
        ("day_of_year", "Day of year"),
        ("countdown", "Countdown to a date"),
    ],
    builder=_space_frames,
    options_schema={
        "countdown_label": {"type": "text", "label": "Countdown label",
                            "default": "countdown", "placeholder": "HALVING"},
        "countdown_date": {"type": "text", "label": "Countdown date",
                           "default": "", "placeholder": "2028-03-26"},
    },
    validate=_validate,
)
