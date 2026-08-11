"""Weather & sky for the user's city (Open-Meteo + USNO, keyless).

The original temp/conditions frames are unchanged. The one-call Open-Meteo
bundle adds UV, wind, humidity, rain % today, minutes-to-sunset and day
length; the separate air-quality hostname adds PM2.5 and the European AQI;
the US Naval Observatory serves moon illumination. All reuse the one geocode
(cached a day).
"""

import time
import urllib.parse
from datetime import date

from clock import (LED_AMBER, LED_GREEN, LED_ICE_BLUE, LED_NET_BLUE, LED_RED,
                   LED_WARM_WHITE, show_number_path, show_text_path)
from sources import (CACHE_TTL_S, FORECAST_API, GEOCODE_API, GEOCODE_TTL_S,
                     _cached, _fit_line, _frame, _http_json, is_offline,
                     register_source)

AIR_API = "https://air-quality-api.open-meteo.com/v1/air-quality"
USNO_API = "https://aa.usno.navy.mil/api/rstt/oneday"

SYNTHETIC_GEO = {"name": "Testville", "lat": -27.47, "lon": 153.03,
                 "country": "Australia"}
SYNTHETIC_GEO_MATCHES = [
    {"name": "Testville", "admin1": "Queensland", "country": "Australia",
     "country_code": "AU", "latitude": -27.47, "longitude": 153.03},
    {"name": "Testville", "admin1": "Ohio", "country": "United States",
     "country_code": "US", "latitude": 40.24, "longitude": -83.35},
]
SYNTHETIC_WEATHER = {"temp": 21.4, "code": 2}


def _synthetic_bundle():
    now = time.time()
    return {"uv": 7.2, "wind": 18.0, "humidity": 64, "rain": 80,
            "sunrise": now - 5 * 3600, "sunset": now + 94 * 60,
            "utc_offset": 36000}  # AEST, so the sunset clock time is plausible


SYNTHETIC_AIR = {"pm2_5": 6.1, "aqi": 22}
SYNTHETIC_MOON = 94

# WMO weather codes -> a word that fits the 7-slot text frame
_WMO = [
    ((0,), "CLEAR"),
    ((1, 2), "FAIR"),
    ((3,), "CLOUDY"),
    ((45, 48), "FOG"),
    ((51, 53, 55, 56, 57), "DRIZZLE"),
    ((61, 63, 65, 66, 67, 80, 81, 82), "RAIN"),
    ((71, 73, 75, 77, 85, 86), "SNOW"),
    ((95, 96, 99), "STORM"),
]


def _sunset_prefix(opt):
    """The 2-char sunset label the user picked: SS (sunset), SD (sundown), DK
    (dusk), or blank. Defaults to SS when unset; anything unrecognised -> blank
    (the bare time)."""
    if opt is None:
        return "SS"
    v = str(opt).strip().upper()
    return v if v in ("SS", "SD", "DK") else ""


def _coded(code, value):
    """Code at the left edge, reading at the right edge of seven slots."""
    reading = str(int(round(float(value))))
    return f"{code:<{7 - len(reading)}}{reading}"


def wmo_word(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    for codes, word in _WMO:
        if code in codes:
            return word
    return "WEATHER"


def geocode(city):
    """City name -> {name, lat, lon, country} or None. Cached a day."""
    city = (city or "").strip()
    if not city:
        return None
    if is_offline():
        return dict(SYNTHETIC_GEO)

    def fetch():
        d = _http_json(GEOCODE_API + "?" + urllib.parse.urlencode(
            {"name": city, "count": 1, "language": "en", "format": "json"}))
        try:
            r = d["results"][0]
            return {"name": r["name"], "lat": float(r["latitude"]),
                    "lon": float(r["longitude"]),
                    "country": r.get("country", "")}
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    return _cached(f"geo:{city.lower()}", GEOCODE_TTL_S, fetch)


def geocode_search(query, count=8):
    """Typeahead search: query -> a trimmed list of candidate places
    [{name, admin1, country, country_code, latitude, longitude}]. Fails soft
    to [] on any upstream trouble; never raises. The existing geocode() (the
    save-time first-result fallback) is untouched."""
    q = str(query or "").strip()
    if not q:
        return []
    if is_offline():
        return [dict(m) for m in SYNTHETIC_GEO_MATCHES]

    def fetch():
        d = _http_json(GEOCODE_API + "?" + urllib.parse.urlencode(
            {"name": q, "count": count, "language": "en", "format": "json"}))
        rows = d.get("results") if isinstance(d, dict) else None
        if not isinstance(rows, list):
            return None
        out = []
        for r in rows:
            try:
                out.append({"name": str(r["name"]),
                            "admin1": str(r.get("admin1") or ""),
                            "country": str(r.get("country") or ""),
                            "country_code": str(r.get("country_code") or ""),
                            "latitude": float(r["latitude"]),
                            "longitude": float(r["longitude"])})
            except (TypeError, KeyError, ValueError):
                continue
        return out
    return _cached(f"geosearch:{q.lower()}:{int(count)}",
                   GEOCODE_TTL_S, fetch) or []


def weather_current(lat, lon, units="C"):
    """Current weather -> {temp, code} in the requested unit, or None."""
    if is_offline():
        w = dict(SYNTHETIC_WEATHER)
        if units == "F":
            w["temp"] = w["temp"] * 9 / 5 + 32
        return w

    def fetch():
        params = {"latitude": round(float(lat), 4),
                  "longitude": round(float(lon), 4),
                  "current": "temperature_2m,weather_code"}
        if units == "F":
            params["temperature_unit"] = "fahrenheit"
        d = _http_json(FORECAST_API + "?" + urllib.parse.urlencode(params))
        try:
            cur = d["current"]
            return {"temp": float(cur["temperature_2m"]),
                    "code": cur.get("weather_code")}
        except (TypeError, KeyError, ValueError):
            return None
    key = f"wx:{round(float(lat), 4)},{round(float(lon), 4)}:{units}"
    return _cached(key, CACHE_TTL_S, fetch)


def weather_bundle(lat, lon):
    """The one-call extras bundle: UV, wind, humidity, rain % today, and
    sunrise/sunset as UNIX times (timeformat=unixtime sidesteps every
    container-vs-city timezone trap). Or None."""
    if is_offline():
        return _synthetic_bundle()

    def fetch():
        params = {"latitude": round(float(lat), 4),
                  "longitude": round(float(lon), 4),
                  "current": "uv_index,wind_speed_10m,relative_humidity_2m",
                  "daily": "sunrise,sunset,precipitation_probability_max",
                  "timeformat": "unixtime", "timezone": "auto",
                  "forecast_days": 1}
        d = _http_json(FORECAST_API + "?" + urllib.parse.urlencode(params))
        try:
            cur, daily = d["current"], d["daily"]
            # timezone=auto makes Open-Meteo return the city's UTC offset; add
            # it to the unix sunset to read the local wall-clock sunset time.
            return {"uv": float(cur["uv_index"]),
                    "wind": float(cur["wind_speed_10m"]),
                    "humidity": int(cur["relative_humidity_2m"]),
                    "rain": daily["precipitation_probability_max"][0],
                    "sunrise": float(daily["sunrise"][0]),
                    "sunset": float(daily["sunset"][0]),
                    "utc_offset": int(d.get("utc_offset_seconds") or 0)}
        except (TypeError, KeyError, IndexError, ValueError):
            return None
    key = f"wxb:{round(float(lat), 4)},{round(float(lon), 4)}"
    return _cached(key, 900, fetch)


def air_quality(lat, lon):
    if is_offline():
        return dict(SYNTHETIC_AIR)

    def fetch():
        params = {"latitude": round(float(lat), 4),
                  "longitude": round(float(lon), 4),
                  "current": "pm2_5,european_aqi"}
        d = _http_json(AIR_API + "?" + urllib.parse.urlencode(params))
        try:
            cur = d["current"]
            return {"pm2_5": float(cur["pm2_5"]),
                    "aqi": int(cur["european_aqi"])}
        except (TypeError, KeyError, ValueError):
            return None
    key = f"air:{round(float(lat), 4)},{round(float(lon), 4)}"
    return _cached(key, 3600, fetch)


def moon_illumination(lat, lon):
    """Moon illumination % from the US Naval Observatory (fracillum like
    '94%'). Verbose response, two fields of interest, cached 6h."""
    if is_offline():
        return SYNTHETIC_MOON

    def fetch():
        params = {"date": date.today().isoformat(),
                  "coords": f"{round(float(lat), 2)},{round(float(lon), 2)}"}
        d = _http_json(USNO_API + "?" + urllib.parse.urlencode(params))
        try:
            frac = d["properties"]["data"]["fracillum"]
            return int(str(frac).rstrip("%"))
        except (TypeError, KeyError, ValueError):
            return None
    return _cached(f"moon:{round(float(lat), 2)},{round(float(lon), 2)}",
                   21_600, fetch)


# --------------------------------------------------------------------------- #
# Colours
# --------------------------------------------------------------------------- #

def temp_color(temp, units="C"):
    """Backlight tint by temperature (thresholds in Celsius)."""
    if temp is None:
        return LED_WARM_WHITE
    tc = (temp - 32) * 5 / 9 if units == "F" else temp
    if tc <= 0:
        return LED_ICE_BLUE
    if tc < 15:
        return LED_NET_BLUE
    if tc < 28:
        return LED_GREEN
    if tc < 36:
        return LED_AMBER
    return LED_RED


def _uv_color(uv):
    if uv < 3:
        return LED_GREEN
    if uv < 8:
        return LED_AMBER
    return LED_RED


def _pm_color(pm):
    if pm < 10:
        return LED_GREEN
    if pm < 25:
        return LED_AMBER
    return LED_RED


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #

def _weather_frames(options, wanted=None):
    lat, lon = options.get("lat"), options.get("lon")
    if lat is None or lon is None:
        geo = geocode(options.get("city"))
        if not geo:
            return []
        lat, lon = geo["lat"], geo["lon"]
        options = dict(options, place=geo["name"])
    show_cond = bool(options.get("show_condition"))

    def want(fid):
        if wanted is None:
            return True
        if fid not in wanted:
            return False
        # the legacy conditions toggle keeps gating its frame
        return fid != "weather_condition" or show_cond

    units = "F" if options.get("units") == "F" else "C"
    place = _fit_line(options.get("place") or options.get("city") or "weather")
    frames = []
    add = frames.append

    if want("weather_temp") or want("weather_condition"):
        w = weather_current(lat, lon, units)
        if w and w.get("temp") is not None:
            temp = int(round(w["temp"]))
            color = temp_color(w["temp"], units)
            if want("weather_temp"):
                text = _coded("TEMP", temp)
                add(_frame("weather_temp",
                           show_text_path(text, tl=place, br=f"deg{units}"),
                           text=text, color=color))
            if want("weather_condition"):
                word = wmo_word(w.get("code"))
                if word:
                    add(_frame("weather_condition",
                               show_text_path(word, tl=place, br="right now"),
                               text=word, color=color))

    bundle_wants = ("weather_uv", "weather_wind", "weather_humidity",
                    "weather_rain", "weather_sunset", "weather_daylen")
    if any(want(f) for f in bundle_wants):
        b = weather_bundle(lat, lon)
        if b:
            if want("weather_uv") and b.get("uv") is not None:
                text = _coded("UV", b["uv"])
                add(_frame("weather_uv",
                           show_text_path(text, tl="UV index", br=place),
                           text=text, color=_uv_color(b["uv"])))
            if want("weather_wind") and b.get("wind") is not None:
                wind = int(round(b["wind"]))
                text = _coded("WIND", wind)
                add(_frame("weather_wind",
                           show_text_path(text, tl="wind", br="km/h"),
                           text=text, color=LED_WARM_WHITE))
            if want("weather_humidity") and b.get("humidity") is not None:
                text = _coded("HUM", b["humidity"])
                add(_frame("weather_humidity",
                           show_text_path(text, tl="humidity", br="%"),
                           text=text, color=LED_NET_BLUE))
            if want("weather_rain") and b.get("rain") is not None:
                text = _coded("RAIN", b["rain"])
                add(_frame("weather_rain",
                           show_text_path(text, tl="rain chance", br="% today"),
                           text=text, color=LED_NET_BLUE))
            if want("weather_sunset") and b.get("sunset"):
                # the local wall-clock sunset time, e.g. "SS18:42" - prefix (2
                # chars) + HH:MM fills the 7 slots exactly. Prefix is the user's
                # pick (SS/SD/DK) or blank; blank just shows the bare time.
                lt = time.gmtime(b["sunset"] + (b.get("utc_offset") or 0))
                prefix = _sunset_prefix(options.get("sunset_label"))
                text = f"{prefix}{lt.tm_hour:02d}:{lt.tm_min:02d}"
                add(_frame("weather_sunset",
                           show_text_path(text, tl=place, br="sunset"),
                           text=text, color=LED_AMBER))
            if want("weather_daylen") and b.get("sunrise") and b.get("sunset"):
                hours = (b["sunset"] - b["sunrise"]) / 3600
                if 0 < hours <= 24:
                    val = f"{hours:.1f}"
                    add(_frame("weather_daylen",
                               show_number_path(val, tl="day length",
                                                br="hours"),
                               number=val, color=LED_WARM_WHITE))

    if want("weather_air_pm25") or want("weather_air_aqi"):
        air = air_quality(lat, lon)
        if air:
            if want("weather_air_pm25") and air.get("pm2_5") is not None:
                text = _coded("PM", air["pm2_5"])
                add(_frame("weather_air_pm25",
                           show_text_path(text, tl="PM2.5", br="ug/m3"),
                           text=text, color=_pm_color(air["pm2_5"])))
            if want("weather_air_aqi") and air.get("aqi") is not None:
                text = _coded("AQ", air["aqi"])
                add(_frame("weather_air_aqi",
                           show_text_path(text, tl="air quality", br="EU AQI"),
                           text=text, color=LED_WARM_WHITE))

    if want("weather_moon"):
        moon = moon_illumination(lat, lon)
        if moon is not None:
            text = _coded("MOON", moon)
            add(_frame("weather_moon",
                       show_text_path(text, tl="moon", br="% lit"),
                       text=text, color=LED_WARM_WHITE))
    return frames


register_source(
    "weather",
    label="Weather",
    category="weather",
    description="Current temperature, conditions, UV, wind, humidity, rain "
                "chance, sunset, day length, air quality and moon phase for "
                "your city via Open-Meteo + USNO (keyless).",
    frames=[
        ("weather_temp", "Temperature"),
        ("weather_condition", "Conditions"),
        ("weather_uv", "UV index"),
        ("weather_wind", "Wind speed"),
        ("weather_humidity", "Humidity"),
        ("weather_rain", "Rain chance today"),
        ("weather_sunset", "Sunset time"),
        ("weather_daylen", "Day length"),
        ("weather_air_pm25", "Air quality (PM2.5)"),
        ("weather_air_aqi", "Air quality (AQI)"),
        ("weather_moon", "Moon illumination"),
    ],
    builder=_weather_frames,
    options_schema={
        "city": {"type": "text", "label": "City", "default": ""},
        "units": {"type": "select", "label": "Units",
                  "choices": ["C", "F"], "default": "C"},
        "sunset_label": {"type": "select", "label": "Sunset label",
                         "choices": [{"id": "SS", "label": "Sunset (SS)"},
                                     {"id": "SD", "label": "Sundown (SD)"},
                                     {"id": "DK", "label": "Dusk (DK)"},
                                     {"id": "", "label": "None (time only)"}],
                         "default": "SS"},
        "show_condition": {"type": "bool", "label": "Also show conditions",
                           "default": False},
    },
)
