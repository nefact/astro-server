import glob
import math
import os
import tempfile
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import swisseph as swe
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from kerykeion import AstrologicalSubject
from pydantic import BaseModel, Field

try:
    from kerykeion import KerykeionChartSVG
except ImportError:
    KerykeionChartSVG = None

app = FastAPI(
    title="Astro Server",
    description="Astrology & Human Design calculation service for Custom GPT",
    version="8.0",
)

GEONAMES_USERNAME = os.environ.get("GEONAMES_USERNAME", "")
API_KEY = os.environ.get("API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "https://astro-server-3f67.onrender.com")


def verify_api_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# =====================================================
# Input models
# =====================================================

class BirthData(BaseModel):
    name: str
    year: int = Field(ge=1000, le=2100)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    city: str
    nation: str = ""


class BirthDataCoords(BaseModel):
    name: str
    year: int = Field(ge=1000, le=2100)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    tz_str: str
    city: str = "Custom location"


class AstroCartographyRequest(BirthDataCoords):
    check_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    check_lng: Optional[float] = Field(default=None, ge=-180, le=180)
    check_name: str = "checked location"


# =====================================================
# Western astrology (Kerykeion)
# =====================================================

def planet(s, attr):
    return getattr(s, attr, None)


def chart_image_url(s: AstrologicalSubject, name: str) -> str:
    params = {
        "name": name,
        "year": s.year, "month": s.month, "day": s.day,
        "hour": s.hour, "minute": s.minute,
        "lat": s.lat, "lng": s.lng, "tz_str": s.tz_str,
    }
    return f"{BASE_URL}/chart.svg?" + urllib.parse.urlencode(params)


def build_response(s: AstrologicalSubject, name: str) -> dict:
    return {
        "location_check": {
            "resolved_city": s.city,
            "country": s.nation,
            "lat": s.lat,
            "lng": s.lng,
            "timezone": s.tz_str,
        },
        "planets": {
            "sun": s.sun, "moon": s.moon, "mercury": s.mercury,
            "venus": s.venus, "mars": s.mars, "jupiter": s.jupiter,
            "saturn": s.saturn, "uranus": s.uranus,
            "neptune": s.neptune, "pluto": s.pluto,
        },
        "points": {
            "true_node": planet(s, "true_node"),
            "mean_node": planet(s, "mean_node"),
            "chiron": planet(s, "chiron"),
        },
        "ascendant": s.first_house,
        "houses": {
            "1": s.first_house, "2": s.second_house,
            "3": s.third_house, "4": s.fourth_house,
            "5": s.fifth_house, "6": s.sixth_house,
            "7": s.seventh_house, "8": s.eighth_house,
            "9": s.ninth_house, "10": s.tenth_house,
            "11": s.eleventh_house, "12": s.twelfth_house,
        },
        "chart_image_url": chart_image_url(s, name),
    }


@app.post("/natal_chart", dependencies=[Depends(verify_api_key)])
def natal_chart(data: BirthData):
    """Calculate a natal chart by city name (resolved via GeoNames).
    Always verify location_check in the response. If the resolved
    city or timezone is wrong, call /natal_chart_coords instead."""
    try:
        s = AstrologicalSubject(
            data.name, data.year, data.month, data.day,
            data.hour, data.minute, data.city, data.nation,
            geonames_username=GEONAMES_USERNAME,
        )
         return build_response(s, data.name)
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Could not calculate chart for city '{data.city}': {e}. "
                "Try the Latin spelling with a country code (nation), "
                "or call /natal_chart_coords with coordinates and a timezone."
            ),
        )


@app.post("/natal_chart_coords", dependencies=[Depends(verify_api_key)])
def natal_chart_coords(data: BirthDataCoords):
    """Reliable calculation without geocoding: latitude, longitude
    and timezone are provided directly."""
    try:
        s = AstrologicalSubject(
            data.name, data.year, data.month, data.day,
            data.hour, data.minute,
            lat=data.lat, lng=data.lng, tz_str=data.tz_str,
            city=data.city, online=False,
        )
        return build_response(s, data.name)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Calculation error: {e}")


@app.get("/chart.svg")
def chart_svg(
    name: str = Query(default="Chart"),
    year: int = Query(ge=1000, le=2100),
    month: int = Query(ge=1, le=12),
    day: int = Query(ge=1, le=31),
    hour: int = Query(ge=0, le=23),
    minute: int = Query(ge=0, le=59),
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    tz_str: str = Query(),
):
    """Render the natal chart wheel as an SVG image (stateless)."""
    if KerykeionChartSVG is None:
        raise HTTPException(status_code=501, detail="Chart drawing unavailable.")
    try:
        s = AstrologicalSubject(
            name, year, month, day, hour, minute,
            lat=lat, lng=lng, tz_str=tz_str, city="", online=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            chart = KerykeionChartSVG(s, new_output_directory=tmp)
            chart.makeSVG()
            files = glob.glob(os.path.join(tmp, "*.svg"))
            if not files:
                raise RuntimeError("SVG file was not produced")
            with open(files[0], "r", encoding="utf-8") as f:
                svg = f.read()
        return Response(content=svg, media_type="image/svg+xml")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Chart drawing error: {e}")


# =====================================================
# Human Design
# =====================================================
# Gate wheel: 64 gates in zodiacal order, starting at
# absolute longitude 302.0 (02°00' Aquarius = start of Gate 41).
# Each gate spans 5.625°, each line 0.9375°.

GATE_WHEEL = [
    41, 19, 13, 49, 30, 55, 37, 63, 22, 36, 25, 17, 21, 51, 42, 3,
    27, 24, 2, 23, 8, 20, 16, 35, 45, 12, 15, 52, 39, 53, 62, 56,
    31, 33, 7, 4, 29, 59, 40, 64, 47, 6, 46, 18, 48, 57, 32, 50,
    28, 44, 1, 43, 14, 34, 9, 5, 26, 11, 10, 58, 38, 54, 61, 60,
]
WHEEL_START = 302.0
GATE_SPAN = 5.625
LINE_SPAN = GATE_SPAN / 6

CENTERS = {
    "Head": [64, 61, 63],
    "Ajna": [47, 24, 4, 17, 43, 11],
    "Throat": [62, 23, 56, 35, 12, 45, 33, 8, 31, 20, 16],
    "G": [1, 13, 25, 46, 2, 15, 10, 7],
    "Heart": [21, 40, 26, 51],
    "Sacral": [34, 5, 14, 29, 59, 9, 3, 42, 27],
    "Spleen": [48, 57, 44, 50, 32, 28, 18],
    "SolarPlexus": [36, 22, 37, 6, 49, 55, 30],
    "Root": [53, 60, 52, 19, 39, 41, 58, 38, 54],
}
GATE_TO_CENTER = {g: c for c, gates in CENTERS.items() for g in gates}

CHANNELS = [
    (1, 8), (2, 14), (3, 60), (4, 63), (5, 15), (6, 59), (7, 31),
    (9, 52), (10, 20), (10, 34), (10, 57), (11, 56), (12, 22),
    (13, 33), (16, 48), (17, 62), (18, 58), (19, 49), (20, 34),
    (20, 57), (21, 45), (23, 43), (24, 61), (25, 51), (26, 44),
    (27, 50), (28, 38), (29, 46), (30, 41), (32, 54), (34, 57),
    (35, 36), (37, 40), (39, 55), (42, 53), (47, 64),
]

MOTORS = {"Sacral", "SolarPlexus", "Heart", "Root"}

SWE_PLANETS = [
    ("sun", swe.SUN), ("moon", swe.MOON), ("mercury", swe.MERCURY),
    ("venus", swe.VENUS), ("mars", swe.MARS), ("jupiter", swe.JUPITER),
    ("saturn", swe.SATURN), ("uranus", swe.URANUS),
    ("neptune", swe.NEPTUNE), ("pluto", swe.PLUTO),
    ("north_node", swe.TRUE_NODE),
]


def lon_at(jd: float, body: int) -> float:
    res, _ = swe.calc_ut(jd, body)
    return res[0] % 360.0


def gate_line(lon: float):
    pos = (lon - WHEEL_START) % 360.0
    idx = int(pos // GATE_SPAN)
    line = int((pos % GATE_SPAN) // LINE_SPAN) + 1
    return GATE_WHEEL[idx], line


def activations_at(jd: float) -> dict:
    out = {}
    sun_lon = lon_at(jd, swe.SUN)
    out["sun"] = sun_lon
    out["earth"] = (sun_lon + 180.0) % 360.0
    for key, body in SWE_PLANETS:
        if key == "sun":
            continue
        out[key] = lon_at(jd, body)
    out["south_node"] = (out["north_node"] + 180.0) % 360.0
    return {
        k: {"longitude": round(v, 4), "gate": gate_line(v)[0],
            "line": gate_line(v)[1]}
        for k, v in out.items()
    }


def find_design_jd(jd_birth: float) -> float:
    """Moment when the Sun was exactly 88 degrees of solar arc
    before its natal position."""
    target = (lon_at(jd_birth, swe.SUN) - 88.0) % 360.0
    jd = jd_birth - 88.0 / 0.9856
    for _ in range(50):
        diff = ((target - lon_at(jd, swe.SUN) + 180.0) % 360.0) - 180.0
        if abs(diff) < 1e-7:
            break
        jd += diff / 0.9856
    return jd


def compute_activations(data: BirthDataCoords):
    """Shared engine for Human Design and Gene Keys: returns
    (ut, jd_birth, jd_design, personality, design)."""
    try:
        tz = ZoneInfo(data.tz_str)
    except Exception:
        raise ValueError(
            f"Unknown timezone '{data.tz_str}'. Use IANA format, "
            "e.g. 'Europe/Moscow', 'Asia/Shanghai' - not 'UTC+3'."
        )
    local = datetime(data.year, data.month, data.day,
                     data.hour, data.minute, tzinfo=tz)
    ut = local.astimezone(ZoneInfo("UTC"))
    jd_birth = swe.julday(ut.year, ut.month, ut.day,
                          ut.hour + ut.minute / 60 + ut.second / 3600)
    jd_design = find_design_jd(jd_birth)
    personality = activations_at(jd_birth)
    design = activations_at(jd_design)
    return ut, jd_birth, jd_design, personality, design


def compute_human_design(data: BirthDataCoords) -> dict:
    ut, jd_birth, jd_design, personality, design = compute_activations(data)

    active_gates = {v["gate"] for v in personality.values()} | \
                   {v["gate"] for v in design.values()}

    defined_channels = [
        f"{a}-{b}" for a, b in CHANNELS
        if a in active_gates and b in active_gates
    ]

    center_edges = []
    defined_centers = set()
    for ch in defined_channels:
        a, b = (int(x) for x in ch.split("-"))
        ca, cb = GATE_TO_CENTER[a], GATE_TO_CENTER[b]
        defined_centers.update([ca, cb])
        center_edges.append((ca, cb))

    def reachable(start: str) -> set:
        seen, stack = {start}, [start]
        while stack:
            node = stack.pop()
            for x, y in center_edges:
                nxt = y if x == node else x if y == node else None
                if nxt and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    motor_to_throat = any(
        "Throat" in reachable(m) for m in MOTORS if m in defined_centers
    ) if "Throat" in defined_centers else False

    sacral = "Sacral" in defined_centers

    if not defined_centers:
        hd_type, strategy = "Reflector", "Wait a lunar cycle"
    elif sacral and motor_to_throat:
        hd_type, strategy = "Manifesting Generator", "Wait to respond"
    elif sacral:
        hd_type, strategy = "Generator", "Wait to respond"
    elif motor_to_throat:
        hd_type, strategy = "Manifestor", "Inform before acting"
    else:
        hd_type, strategy = "Projector", "Wait for the invitation"

    if "SolarPlexus" in defined_centers:
        authority = "Emotional (Solar Plexus)"
    elif sacral:
        authority = "Sacral"
    elif "Spleen" in defined_centers:
        authority = "Splenic"
    elif "Heart" in defined_centers:
        authority = ("Ego (Manifested)" if "Throat" in reachable("Heart")
                     else "Ego (Projected)")
    elif "G" in defined_centers:
        authority = "Self-Projected"
    elif hd_type == "Reflector":
        authority = "Lunar"
    else:
        authority = "Mental/Environmental"

    remaining = set(defined_centers)
    components = 0
    while remaining:
        components += 1
        remaining -= reachable(next(iter(remaining)))
    definition = {0: "None", 1: "Single", 2: "Split",
                  3: "Triple Split", 4: "Quadruple Split"}.get(
        components, f"{components} components")

    profile = f"{personality['sun']['line']}/{design['sun']['line']}"

    RIGHT_ANGLE = {"1/3", "1/4", "2/4", "2/5", "3/5", "3/6", "4/6"}
    LEFT_ANGLE = {"5/1", "5/2", "6/2", "6/3"}
    if profile in RIGHT_ANGLE:
        cross_angle = "Right Angle (personal destiny)"
    elif profile == "4/1":
        cross_angle = "Juxtaposition (fixed fate)"
    elif profile in LEFT_ANGLE:
        cross_angle = "Left Angle (transpersonal destiny)"
    else:
        cross_angle = "Unknown"

    return {
        "verification_note": (
            "Verify this chart against an official Human Design source "
            "(e.g. jovianarchive.com) before treating type/authority/"
            "profile as reliable. Mark as preliminary until verified."
        ),
        "birth_utc": ut.isoformat(),
        "design_utc_julian_day": round(jd_design, 6),
        "type": hd_type,
        "strategy": strategy,
        "authority": authority,
        "profile": profile,
        "definition": definition,
        "incarnation_cross_angle": cross_angle,
        "confidence": {
            "level": "preliminary",
            "reasons": [
                "birth time provided to minute precision",
                "chart not yet verified against an official "
                "Human Design source",
            ],
        },
        "defined_centers": sorted(defined_centers),
        "open_centers": sorted(set(CENTERS) - defined_centers),
        "defined_channels": sorted(defined_channels),
        "incarnation_cross_gates": {
            "personality_sun": personality["sun"]["gate"],
            "personality_earth": personality["earth"]["gate"],
            "design_sun": design["sun"]["gate"],
            "design_earth": design["earth"]["gate"],
        },
        "personality_activations": personality,
        "design_activations": design,
    }


@app.post("/human_design", dependencies=[Depends(verify_api_key)])
def human_design(data: BirthDataCoords):
    """Calculate a Human Design bodygraph: type, strategy, authority,
    profile, definition, centers, channels and gate activations
    (Personality and Design). Requires coordinates and timezone;
    if you only have a city, call /natal_chart first and take
    lat/lng/tz from location_check."""
    try:
        return compute_human_design(data)
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"Human Design calculation error: {e}")


@app.post("/gene_keys", dependencies=[Depends(verify_api_key)])
def gene_keys(data: BirthDataCoords):
    """Calculate the Gene Keys Activation Sequence: Life's Work,
    Evolution, Radiance and Purpose gates (same engine as the Human
    Design incarnation cross). Requires coordinates and timezone."""
    try:
        ut, jd_birth, jd_design, personality, design = compute_activations(data)
        return {
            "verification_note": (
                "Gate numbers are computed from the same verified "
                "engine as /human_design. Only the core Activation "
                "Sequence is included; Venus and Pearl Sequences are "
                "not implemented yet pending verification against a "
                "reference source."
            ),
            "confidence": {
                "level": "reliable (reuses verified Human Design engine)",
            },
            "activation_sequence": {
                "life_work": personality["sun"],
                "evolution": personality["earth"],
                "radiance": design["sun"],
                "purpose": design["earth"],
            },
            "note_for_model": (
                "These are gate numbers only (the calculated layer). "
                "Provide the Gene Keys themes/meanings for each gate "
                "from your own general knowledge, clearly marked as "
                "the symbolic/interpretive layer, not as РАСЧЁТ."
            ),
        }
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"Gene Keys calculation error: {e}")


# =====================================================
# AstroCartography
# =====================================================

def equatorial(jd: float, body: int):
    """Right ascension and declination in degrees."""
    res, _ = swe.calc_ut(jd, body, swe.FLG_SWIEPH | swe.FLG_EQUATORIAL)
    return res[0], res[1]


def norm180(x: float) -> float:
    x = x % 360.0
    return x - 360.0 if x > 180.0 else x


def mc_ic_longitude(ra_deg: float, gst_deg: float):
    mc = norm180(ra_deg - gst_deg)
    ic = norm180(mc + 180.0)
    return mc, ic


def rise_set_curve(ra_deg: float, dec_deg: float, gst_deg: float,
                   lat_step: int = 5):
    """Sampled AC (rising) and DC (setting) curves. Skips latitudes
    where the planet is circumpolar or never rises (no solution)."""
    rise_pts, set_pts = [], []
    dec = math.radians(dec_deg)
    for lat_i in range(-65, 66, lat_step):
        phi = math.radians(lat_i)
        cos_h0 = -math.tan(phi) * math.tan(dec)
        if abs(cos_h0) > 1:
            continue
        h0 = math.degrees(math.acos(cos_h0))
        rise_pts.append({"lat": lat_i,
                         "lng": round(norm180((ra_deg - h0) - gst_deg), 3)})
        set_pts.append({"lat": lat_i,
                        "lng": round(norm180((ra_deg + h0) - gst_deg), 3)})
    return rise_pts, set_pts


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def meridian_distance_km(check_lat, check_lng, line_lng) -> float:
    dlon = norm180(check_lng - line_lng)
    return abs(math.radians(dlon)) * 6371.0 * math.cos(math.radians(check_lat))


@app.post("/astrocartography", dependencies=[Depends(verify_api_key)])
def astrocartography(data: AstroCartographyRequest):
    """Calculate astrocartography lines: MC/IC meridians and AC/DC
    curves for all planets. Optionally pass check_lat/check_lng to
    get distances from a place to each line. Requires coordinates
    and timezone; this module is new and not yet cross-verified."""
    try:
        tz = ZoneInfo(data.tz_str)
    except Exception:
        raise HTTPException(status_code=422, detail=(
            f"Unknown timezone '{data.tz_str}'. Use IANA format, "
            "e.g. 'Europe/Moscow' - not 'UTC+3'."
        ))
    try:
        local = datetime(data.year, data.month, data.day,
                         data.hour, data.minute, tzinfo=tz)
        ut = local.astimezone(ZoneInfo("UTC"))
        jd = swe.julday(ut.year, ut.month, ut.day,
                        ut.hour + ut.minute / 60 + ut.second / 3600)
        gst_deg = swe.sidtime(jd) * 15.0

        lines = {}
        nearest = []
        for key, body in SWE_PLANETS:
            ra, dec = equatorial(jd, body)
            mc, ic = mc_ic_longitude(ra, gst_deg)
            ac_line, dc_line = rise_set_curve(ra, dec, gst_deg)
            lines[key] = {
                "mc_longitude": round(mc, 3),
                "ic_longitude": round(ic, 3),
                "ac_line": ac_line,
                "dc_line": dc_line,
            }
            if data.check_lat is not None and data.check_lng is not None:
                nearest.append({"planet": key, "line": "MC",
                    "distance_km": round(meridian_distance_km(
                        data.check_lat, data.check_lng, mc), 1)})
                nearest.append({"planet": key, "line": "IC",
                    "distance_km": round(meridian_distance_km(
                        data.check_lat, data.check_lng, ic), 1)})
                if ac_line:
                    d = min(haversine_km(data.check_lat, data.check_lng,
                                         p["lat"], p["lng"]) for p in ac_line)
                    nearest.append({"planet": key, "line": "AC",
                                    "distance_km": round(d, 1)})
                if dc_line:
                    d = min(haversine_km(data.check_lat, data.check_lng,
                                         p["lat"], p["lng"]) for p in dc_line)
                    nearest.append({"planet": key, "line": "DC",
                                    "distance_km": round(d, 1)})

        result = {
            "verification_note": (
                "New, not yet cross-verified module. Check against a "
                "known astrocartography source (e.g. astro.com AstroClick "
                "Travel) before treating as reliable. AC/DC curves are "
                "sampled every 5 degrees of latitude, so distances to "
                "these lines are approximate."
            ),
            "confidence": {"level": "unverified - new module"},
            "lines": lines,
        }
        if data.check_lat is not None and data.check_lng is not None:
            nearest.sort(key=lambda x: x["distance_km"])
            result["nearest_lines_to_check_location"] = {
                "location": data.check_name,
                "lat": data.check_lat,
                "lng": data.check_lng,
                "closest_lines": nearest[:10],
            }
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"AstroCartography calculation error: {e}")


# =====================================================
# Vedic Astrology (Jyotish) - sidereal zodiac, Lahiri ayanamsha
# =====================================================

RASHIS = ["Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
         "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"]

NAKSHATRAS = ["Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra",
    "Punarvasu", "Pushya", "Ashlesha", "Magha", "Purva Phalguni",
    "Uttara Phalguni", "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha",
    "Jyeshtha", "Mula", "Purva Ashadha", "Uttara Ashadha", "Shravana",
    "Dhanishta", "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada", "Revati"]

NAKSHATRA_SPAN = 360.0 / 27
PADA_SPAN = NAKSHATRA_SPAN / 4

DASHA_ORDER = ["Ketu", "Venus", "Sun", "Moon", "Mars", "Rahu",
              "Jupiter", "Saturn", "Mercury"]
DASHA_YEARS = {"Ketu": 7, "Venus": 20, "Sun": 6, "Moon": 10, "Mars": 7,
              "Rahu": 18, "Jupiter": 16, "Saturn": 19, "Mercury": 17}

# Days per dasha-year: classical Vimshottari software commonly uses the
# Julian year (365.25 days), not the Gregorian mean year (365.2425) or
# the sidereal year (365.25636). Using a different convention shifts
# mahadasha transition dates by roughly a day per decade - noted in the
# response so this is diagnosable if dates don't match a reference tool.
DASHA_DAYS_PER_YEAR = 365.25

VEDIC_BODIES = [
    ("sun", swe.SUN), ("moon", swe.MOON), ("mercury", swe.MERCURY),
    ("venus", swe.VENUS), ("mars", swe.MARS), ("jupiter", swe.JUPITER),
    ("saturn", swe.SATURN), ("uranus", swe.URANUS),
    ("neptune", swe.NEPTUNE), ("pluto", swe.PLUTO),
]


def tropical_lon_and_speed(jd: float, body: int):
    res, _ = swe.calc_ut(jd, body, swe.FLG_SWIEPH)
    return res[0] % 360.0, res[3]


def ayanamsha_deg(jd: float) -> float:
    swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
    return swe.get_ayanamsa_ut(jd)


def sign_and_degree(sid_lon: float):
    idx = int(sid_lon // 30) % 12
    return RASHIS[idx], idx, round(sid_lon % 30, 3)


def nakshatra_info(sid_lon: float):
    idx = int(sid_lon // NAKSHATRA_SPAN) % 27
    pos = sid_lon % NAKSHATRA_SPAN
    pada = int(pos // PADA_SPAN) + 1
    lord = DASHA_ORDER[idx % 9]
    fraction = pos / NAKSHATRA_SPAN
    return NAKSHATRAS[idx], pada, lord, fraction


def build_vimshottari(start_lord: str, fraction_traversed: float,
                      birth_utc: datetime, min_years: float = 125.0):
    sequence = []
    balance = DASHA_YEARS[start_lord] * (1 - fraction_traversed)
    cursor = birth_utc
    end = cursor + timedelta(days=balance * DASHA_DAYS_PER_YEAR)
    sequence.append({
        "lord": start_lord,
        "years": round(balance, 3),
        "start": cursor.isoformat(),
        "end": end.isoformat(),
    })
    total = balance
    cursor = end
    idx = DASHA_ORDER.index(start_lord)
    i = 1
    while total < min_years:
        lord = DASHA_ORDER[(idx + i) % 9]
        years = DASHA_YEARS[lord]
        end = cursor + timedelta(days=years * DASHA_DAYS_PER_YEAR)
        sequence.append({
            "lord": lord, "years": years,
            "start": cursor.isoformat(), "end": end.isoformat(),
        })
        total += years
        cursor = end
        i += 1
    return sequence


def point_details(sid_lon: float, asc_sign_idx: int) -> dict:
    sign, sign_idx, deg = sign_and_degree(sid_lon)
    nak, pada, lord, frac = nakshatra_info(sid_lon)
    house = ((sign_idx - asc_sign_idx) % 12) + 1
    return {
        "sidereal_longitude": round(sid_lon, 3),
        "sign": sign, "degree_in_sign": deg,
        "nakshatra": nak, "pada": pada,
        "nakshatra_lord": lord, "house": house,
    }


@app.post("/vedic_chart", dependencies=[Depends(verify_api_key)])
def vedic_chart(data: BirthDataCoords):
    """Calculate a Vedic (Jyotish) chart: sidereal planet positions
    (Lahiri ayanamsha), nakshatras/padas, whole-sign houses, and the
    Vimshottari Dasha sequence from birth. Requires coordinates and
    timezone; this module is new and not yet cross-verified."""
    try:
        tz = ZoneInfo(data.tz_str)
    except Exception:
        raise HTTPException(status_code=422, detail=(
            f"Unknown timezone '{data.tz_str}'. Use IANA format, "
            "e.g. 'Europe/Moscow' - not 'UTC+3'."
        ))
    try:
        local = datetime(data.year, data.month, data.day,
                         data.hour, data.minute, tzinfo=tz)
        ut = local.astimezone(ZoneInfo("UTC"))
        jd = swe.julday(ut.year, ut.month, ut.day,
                        ut.hour + ut.minute / 60 + ut.second / 3600)

        ayan = ayanamsha_deg(jd)

        cusps, ascmc = swe.houses_ex(jd, data.lat, data.lng, b"P")
        asc_sid = (ascmc[0] - ayan) % 360.0
        asc_sign, asc_sign_idx, asc_deg = sign_and_degree(asc_sid)
        asc_nak, asc_pada, _, _ = nakshatra_info(asc_sid)

        planets = {}
        for key, body in VEDIC_BODIES:
            trop, speed = tropical_lon_and_speed(jd, body)
            sid = (trop - ayan) % 360.0
            details = point_details(sid, asc_sign_idx)
            details["retrograde"] = speed < 0
            planets[key] = details

        # Lunar nodes: both True Node and Mean Node are computed and
        # labeled, since Vedic software commonly differs on which one
        # it uses for Rahu/Ketu (a frequent source of small mismatches).
        true_node_trop, _ = tropical_lon_and_speed(jd, swe.TRUE_NODE)
        mean_node_trop, _ = tropical_lon_and_speed(jd, swe.MEAN_NODE)
        true_rahu_sid = (true_node_trop - ayan) % 360.0
        mean_rahu_sid = (mean_node_trop - ayan) % 360.0

        planets["rahu_true_node"] = point_details(true_rahu_sid, asc_sign_idx)
        planets["ketu_true_node"] = point_details(
            (true_rahu_sid + 180.0) % 360.0, asc_sign_idx)
        planets["rahu_mean_node"] = point_details(mean_rahu_sid, asc_sign_idx)
        planets["ketu_mean_node"] = point_details(
            (mean_rahu_sid + 180.0) % 360.0, asc_sign_idx)

        # Dasha is computed from the Moon's nakshatra - node choice
        # does not affect this part.
        moon_nak, moon_pada, moon_lord, moon_frac = nakshatra_info(
            planets["moon"]["sidereal_longitude"])
        dasha_sequence = build_vimshottari(moon_lord, moon_frac, ut)

        houses_whole_sign = {
            str(h + 1): RASHIS[(asc_sign_idx + h) % 12] for h in range(12)
        }

        return {
            "verification_note": (
                "New, not yet cross-verified module. Check against a "
                "known Vedic calculator (e.g. drikpanchang.com or "
                "Prokerala) before treating as reliable. Two conventions "
                "are exposed explicitly because different software "
                "disagrees on them: rahu/ketu are given as both "
                "true_node and mean_node (compare to whichever your "
                "reference uses); mahadasha dates use 365.25 days/year "
                "(Julian year), which may drift by about a day per "
                "decade versus tools using a different year length. "
                "The Ascendant is assumed to be house-system-independent "
                "(same rising degree regardless of house system)."
            ),
            "confidence": {"level": "unverified - new module"},
            "ayanamsha": {"mode": "Lahiri", "value_deg": round(ayan, 5)},
            "ascendant": {
                "sidereal_longitude": round(asc_sid, 3),
                "sign": asc_sign, "degree_in_sign": asc_deg,
                "nakshatra": asc_nak, "pada": asc_pada,
            },
            "houses_whole_sign": houses_whole_sign,
            "planets": planets,
            "vimshottari_dasha": {
                "birth_nakshatra": moon_nak,
                "birth_nakshatra_lord": moon_lord,
                "fraction_of_nakshatra_elapsed": round(moon_frac, 4),
                "mahadasha_sequence": dasha_sequence,
                "note": (
                    "Only Mahadasha (major period) level is computed. "
                    "Antardasha (sub-periods) are not implemented yet. "
                    "Divisional charts (e.g. D9 Navamsha) are not "
                    "implemented yet either - this is the D1 Rashi "
                    "chart only."
                ),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"Vedic chart calculation error: {e}")


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """<h1>Privacy Policy</h1>
    <p>This service calculates astrological and Human Design charts
    from birth data provided by the user. Data is processed in memory
    only and is not stored, logged, or shared with third parties.</p>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
