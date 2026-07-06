import glob
import os
import tempfile
import urllib.parse
from datetime import datetime
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
    version="5.2",
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


def compute_human_design(data: BirthDataCoords) -> dict:
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


# =====================================================

@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """<h1>Privacy Policy</h1>
    <p>This service calculates astrological and Human Design charts
    from birth data provided by the user. Data is processed in memory
    only and is not stored, logged, or shared with third parties.</p>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
