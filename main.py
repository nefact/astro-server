import functools
import glob
import math
import os
import tempfile
import threading
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

SERVER_VERSION = "12.4"

app = FastAPI(
    title="Astro Server",
    description="Multi-system personality calculation service for Custom GPT",
    version=SERVER_VERSION,
)

GEONAMES_USERNAME = os.environ.get("GEONAMES_USERNAME", "")
API_KEY = os.environ.get("API_KEY", "")
BASE_URL = os.environ.get("BASE_URL", "https://astro-server-3f67.onrender.com")


def verify_api_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# =====================================================
# Engine catalog: every system declares its engine, method,
# conventions and verification status. Each JSON response carries
# an "engine" block; GET /systems lists the whole canon.
# =====================================================

SE = "Swiss Ephemeris via pyswisseph"
ENGINES = {
    "western_natal": {
        "system": "Western astrology (natal)",
        "operations": ["natal_chart", "natal_chart_coords", "chart.svg"],
        "engine": "Kerykeion (Python) over " + SE,
        "method": "Tropical zodiac, Placidus houses, true lunar node",
        "verification": "Positions and Ascendant matched an independent "
                        "Swiss Ephemeris WebAssembly build (app team) "
                        "within ~0.005 deg; set SE_EPHE_PATH to Swiss "
                        "data files so all modules share one ephemeris",
        "status": "verified",
    },
    "predictive": {
        "system": "Predictive astrology",
        "operations": ["predictive"],
        "engine": SE + " (direct calls)",
        "method": "Transits to natal with orbs, secondary progressions "
                  "(1 day = 1 tropical year), solar-arc angles, exact "
                  "solar return; Placidus natal houses",
        "verification": "Same engine as the verified natal modules; "
                        "forecast logic covered by offline tests",
        "status": "engine verified, module new",
    },
    "astrocartography": {
        "system": "Astrocartography (relocation)",
        "operations": ["astrocartography"],
        "engine": SE + " (equatorial coordinates) + IAU sidereal time",
        "method": "MC/IC meridians exact; AC/DC curves sampled every 5 "
                  "deg of latitude",
        "verification": "Saturn MC vs astro.com ~1 km; Sun MC vs "
                        "independent IAU formula 0.001 deg; Sun AC vs "
                        "astro.com ~5 km",
        "status": "verified",
    },
    "vedic": {
        "system": "Vedic astrology (Jyotish) incl. D7",
        "operations": ["vedic_chart", "vedic_d7"],
        "engine": SE + " with Lahiri ayanamsha (SIDM_LAHIRI)",
        "method": "Sidereal D1, whole-sign houses, nakshatras/padas, "
                  "Vimshottari mahadashas (365.25-day years), true and "
                  "mean nodes; D7 by the Parashara odd/even rule",
        "verification": "Ayanamsha vs independent precession estimate "
                        "0.01 deg; Moon nakshatra, pada and rashi matched "
                        "Prokerala; D7 rule tested on all 84 divisions",
        "status": "verified",
    },
    "bazi": {
        "system": "BaZi (Four Pillars)",
        "operations": ["bazi_chart"],
        "engine": "Own implementation; solar terms from " + SE,
        "method": "Li Chun year, Jie-term months, continuous day cycle "
                  "from epoch 1900-01-31 = Jia-Chen, Five Tigers/Rats, "
                  "Ten Gods, luck pillars (3 days = 1 year)",
        "verification": "Four pillars matched lunar-python and "
                        "lunar-javascript; luck start age convention "
                        "differs by weeks",
        "status": "verified",
    },
    "ziwei": {
        "system": "Zi Wei Dou Shu",
        "operations": ["ziwei_chart"],
        "engine": "Own implementation; lunisolar calendar from new moons "
                  "and zhongqi computed with " + SE,
        "method": "San He school: Ming/Shen palaces, Na Yin bureau, 14 "
                  "major stars, Chang/Qu/Zuo/You, Si Hua by year stem",
        "verification": "Lunar date matched an independent lunar "
                        "library; Zi Wei position formula passed 5 "
                        "textbook anchors; palaces not yet compared",
        "status": "partially verified",
    },
    "human_design": {
        "system": "Human Design",
        "operations": ["human_design"],
        "engine": "Own implementation over " + SE,
        "method": "Rave mandala gate wheel from 302 deg (Gate 41), "
                  "Design moment at 88 deg of solar arc, true node",
        "verification": "Manifestor 3/5 chart matched Jovian Archive "
                        "fully; Projector 5/1 matched a known profile",
        "status": "verified",
    },
    "gene_keys": {
        "system": "Gene Keys",
        "operations": ["gene_keys"],
        "engine": "Human Design engine (same activations)",
        "method": "Activation Sequence only (Life's Work, Evolution, "
                  "Radiance, Purpose)",
        "verification": "Inherits the verified Human Design engine; not "
                        "independent evidence from Human Design",
        "status": "verified",
    },
    "numerology": {
        "system": "Numerology (Pythagorean)",
        "operations": ["numerology"],
        "engine": "Own implementation (pure arithmetic)",
        "method": "Life Path two methods, master numbers 11/22/33, "
                  "Latin and Russian letter tables, pinnacles, "
                  "challenges, personal cycles",
        "verification": "Arithmetic tested on worked cases",
        "status": "verified arithmetic",
    },
    "destiny_matrix": {
        "system": "Destiny Matrix",
        "operations": ["destiny_matrix"],
        "engine": "Own implementation (pure arithmetic)",
        "method": "22-arcana method: outer points, centre, ancestral "
                  "square, sky/earth, male/female lines, purposes",
        "verification": "Matched three published worked examples",
        "status": "verified",
    },
    "tarot_birth_cards": {
        "system": "Tarot birth cards",
        "operations": ["tarot_birth_cards"],
        "engine": "Own implementation (pure arithmetic)",
        "method": "Mary K. Greer: month + day + year reduced to 22 or less",
        "verification": "Arithmetic tested, incl. the 19 and 22 cases",
        "status": "verified arithmetic",
    },
    "feng_shui": {
        "system": "Feng Shui (Eight Mansions, Nine Star Ki, Flying Stars)",
        "operations": ["feng_shui"],
        "engine": "Own implementation; Li Chun boundary from " + SE,
        "method": "Kua with 5 -> 2/8 rule, 8 directions, Nine Star Ki "
                  "year/month stars, annual flying-star chart, period",
        "verification": "Known annual stars 1990-2026 and Five Yellow "
                        "positions 2024-2026; direction table symmetric",
        "status": "verified",
    },
    "iching": {
        "system": "I Ching (birth hexagram)",
        "operations": ["iching_birth"],
        "engine": "Own implementation; lunar date from the Zi Wei engine",
        "method": "Mei Hua Yi Shu time method: primary, changed and "
                  "nuclear hexagrams, moving line, body/use",
        "verification": "King Wen table passed the pair rule for all 32 "
                        "pairs; method is school-dependent",
        "status": "verified arithmetic",
    },
    "qimen": {
        "system": "Qi Men Dun Jia",
        "operations": ["qimen"],
        "engine": "Own implementation; solar terms from " + SE,
        "method": "Hour chart, Chai Bu method: Fu Tou yuan, Ju table, "
                  "earth/heaven plates, stars, doors, deities, void, horse",
        "verification": "Algorithm matches a published open algorithm "
                        "step by step; upper-yuan Ju numbers match the "
                        "classical verse; no numeric reference chart "
                        "compared yet",
        "status": "method verified, chart not yet compared",
    },
    "big_five": {
        "system": "Big Five personality",
        "operations": ["big_five/items", "big_five"],
        "engine": "Own scoring of the Mini-IPIP (public-domain IPIP)",
        "method": "20 items, 1-5, reverse keying, scale position 0-100 "
                  "(no population norms)",
        "verification": "Scoring key tested",
        "status": "verified scoring",
    },
    "schwartz_values": {
        "system": "Schwartz basic values",
        "operations": ["schwartz_values"],
        "engine": "Own scoring of PVQ-21 (ESS)",
        "method": "10 values, within-person centring, 4 higher-order "
                  "dimensions",
        "verification": "Item mapping and centring tested; check the "
                        "licence before commercial use",
        "status": "verified scoring",
    },
    "palmistry": {
        "system": "Palmistry / dermatoglyphics",
        "operations": [],
        "engine": "none",
        "method": "Requires photographs of the hands; cannot be "
                  "calculated from birth data",
        "verification": "Not applicable",
        "status": "no engine - hypothesis only",
    },
}
SYMBOLIC_LAYERS = ["Tarot readings", "Tree of Sephirot", "Chakras / energy",
                   "Ancestral and karmic scenarios", "Bon symbolic layer",
                   "Akashic records (metaphor)", "Star archetypes (metaphor)"]
APP_DIR = os.path.dirname(os.path.abspath(__file__))
EPHE_DIR = os.path.join(APP_DIR, "ephe")
EPHE_CANDIDATES = (EPHE_DIR, APP_DIR)   # ./ephe folder or next to main.py
EPHE_FILES = ("sepl_18.se1", "semo_18.se1")   # planets and Moon, 1800-2399
EPHE_SOURCE = "https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/"
_EPHE_DOWNLOAD = {"state": "not started", "error": None}
# Swiss Ephemeris keeps global state and set_ephe_path closes its open
# files, so all calculations run one at a time under this lock.
_CALC_LOCK = threading.RLock()


def _ephe_files_present(folder: Optional[str]) -> bool:
    return bool(folder) and all(
        os.path.isfile(os.path.join(folder, f))
        and os.path.getsize(os.path.join(folder, f)) > 100_000
        for f in EPHE_FILES)


def _download_ephemeris():
    """Fetch the official Swiss Ephemeris data files once at startup so
    no manual upload is needed. Runs in the background; until it
    finishes (or if it fails) Swiss Ephemeris uses its Moshier model."""
    import urllib.request
    try:
        _EPHE_DOWNLOAD["state"] = "downloading"
        os.makedirs(EPHE_DIR, exist_ok=True)
        for name in EPHE_FILES:
            target = os.path.join(EPHE_DIR, name)
            if os.path.isfile(target) and os.path.getsize(target) > 100_000:
                continue
            tmp = target + ".part"
            with urllib.request.urlopen(EPHE_SOURCE + name, timeout=60) as r,                     open(tmp, "wb") as f:
                f.write(r.read())
            if os.path.getsize(tmp) < 100_000:
                raise RuntimeError(f"{name}: downloaded file is too small")
            os.replace(tmp, target)
        _EPHE_DOWNLOAD["state"] = "done"
    except Exception as e:
        _EPHE_DOWNLOAD.update(state="failed", error=str(e))


def _local_ephe_dir() -> Optional[str]:
    return next((d for d in EPHE_CANDIDATES if _ephe_files_present(d)), None)


def start_ephemeris_download():
    if os.environ.get("SE_EPHE_PATH") or _local_ephe_dir():
        _EPHE_DOWNLOAD["state"] = "not needed"
        return
    threading.Thread(target=_download_ephemeris, daemon=True).start()


def init_ephemeris() -> Optional[str]:
    """Point Swiss Ephemeris at real data files so every module uses the
    same ephemeris. Order: SE_EPHE_PATH, an ./ephe folder or the files
    next to main.py, Kerykeion's folder. Re-applied before every endpoint call, because
    Kerykeion resets the path whenever it builds a natal chart."""
    path = os.environ.get("SE_EPHE_PATH") or _local_ephe_dir()
    if not path:
        try:
            import kerykeion as _k
            cand = os.path.join(os.path.dirname(_k.__file__), "sweph")
            path = cand if os.path.isdir(cand) else None
        except Exception:
            path = None
    if path:
        swe.set_ephe_path(path)
    return path


def ephemeris_backend() -> dict:
    with _CALC_LOCK:
        path = init_ephemeris()
        try:
            _, flag = swe.calc_ut(2451545.0, swe.SUN, swe.FLG_SWIEPH)
            files = bool(flag & swe.FLG_SWIEPH) and not (flag & swe.FLG_MOSEPH)
        except Exception:
            files = False
    return {
        "backend": ("Swiss Ephemeris data files" if files
                    else "Moshier analytical fallback (no data files)"),
        "ephe_path": path,
        "auto_download": dict(_EPHE_DOWNLOAD),
    }


start_ephemeris_download()


def engine_info(system_id: str) -> dict:
    info = dict(ENGINES[system_id])
    info["server_version"] = SERVER_VERSION
    if "Swiss Ephemeris" in info["engine"] or system_id in (
            "human_design", "gene_keys", "ziwei", "iching"):
        info["ephemeris"] = ephemeris_backend()
    return info


def with_engine(system_id: str):
    """Add the engine block to an endpoint's JSON response."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _CALC_LOCK:
                init_ephemeris()
                result = fn(*args, **kwargs)
                if isinstance(result, dict):
                    result["engine"] = engine_info(system_id)
            return result
        return wrapper
    return deco


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


def build_response(s: AstrologicalSubject, name: str,
                   country_known: bool = True) -> dict:
    return {
        "location_check": {
            "resolved_city": s.city,
            # Coordinate input has no geocoding, so Kerykeion falls back
            # to a placeholder country ("GB"); report null instead.
            "country": s.nation if country_known else None,
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
@with_engine("western_natal")
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
@with_engine("western_natal")
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
        return build_response(s, data.name, country_known=False)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Calculation error: {e}")


@app.get("/chart.svg")
@with_engine("western_natal")       # serialised; SVG responses get no engine block
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
            "Verified on two charts of different types: a Manifestor "
            "3/5 chart matched Jovian Archive on type, strategy, "
            "authority, profile, definition, cross angle and channels; "
            "a Projector 5/1 Splenic chart matched the owner's known "
            "profile. Births near a gate or line boundary are sensitive "
            "to a few minutes of birth time."
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
            "level": "verified engine",
            "reasons": [
                "two charts of different types matched references",
                "birth time precision limits gate/line boundary cases",
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
@with_engine("human_design")
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
@with_engine("gene_keys")
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
@with_engine("astrocartography")
def astrocartography(data: AstroCartographyRequest):
    """Calculate astrocartography lines: MC/IC meridians and AC/DC
    curves for all planets. Optionally pass check_lat/check_lng to
    get distances from a place to each line. Requires coordinates
    and timezone. MC/IC verified against astro.com and an independent
    formula; AC/DC sampled every 5 deg of latitude."""
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
                "Verified: Saturn MC matched astro.com AstroClick "
                "Travel within ~1 km; Sun MC matched an independent "
                "IAU sidereal-time formula to 0.001 deg; Sun AC matched "
                "astro.com within ~5 km. MC/IC meridians are exact. "
                "AC/DC curves are sampled every 5 degrees of latitude, "
                "so distances to those lines are approximate."
            ),
            "confidence": {
                "mc_ic": "verified",
                "ac_dc": "verified formula, approximate distances",
            },
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
    # FLG_SPEED is required: without it Swiss Ephemeris returns zero
    # speed and every planet would be reported as direct.
    res, _ = swe.calc_ut(jd, body, swe.FLG_SWIEPH | swe.FLG_SPEED)
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
@with_engine("vedic")
def vedic_chart(data: BirthDataCoords):
    """Calculate a Vedic (Jyotish) chart: sidereal planet positions
    (Lahiri ayanamsha), nakshatras/padas, whole-sign houses, and the
    Vimshottari Dasha sequence from birth. Requires coordinates and
    timezone. Sidereal layer verified against Prokerala; dasha
    and node conventions are in the response."""
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
                "Sidereal layer verified: Lahiri ayanamsha matched an "
                "independent precession estimate within 0.01 deg, and "
                "Moon nakshatra, pada and rashi matched Prokerala. "
                "Retrograde flags fixed in v10.3 (earlier versions "
                "reported every planet as direct). Two conventions "
                "are exposed explicitly because different software "
                "disagrees on them: rahu/ketu are given as both "
                "true_node and mean_node (compare to whichever your "
                "reference uses); mahadasha dates use 365.25 days/year "
                "(Julian year), which may drift by about a day per "
                "decade versus tools using a different year length. "
                "The Ascendant is assumed to be house-system-independent "
                "(same rising degree regardless of house system)."
            ),
            "confidence": {
                "sidereal_positions": "verified against Prokerala",
                "dasha_dates": "rule verified, exact dates convention-dependent",
            },
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


# =====================================================
# BaZi (Four Pillars of Destiny)
# =====================================================

BAZI_STEMS = ["Jia", "Yi", "Bing", "Ding", "Wu", "Ji", "Geng", "Xin", "Ren", "Gui"]
BAZI_STEM_ELEMENT = ["Wood", "Wood", "Fire", "Fire", "Earth", "Earth",
                     "Metal", "Metal", "Water", "Water"]
BAZI_STEM_YANG = [True, False, True, False, True, False, True, False, True, False]

BAZI_BRANCHES = ["Zi", "Chou", "Yin", "Mao", "Chen", "Si", "Wu", "Wei",
                 "Shen", "You", "Xu", "Hai"]
BAZI_BRANCH_ELEMENT = ["Water", "Earth", "Wood", "Wood", "Earth", "Fire",
                       "Fire", "Earth", "Metal", "Metal", "Earth", "Water"]

BAZI_HIDDEN_STEMS = {
    "Zi": ["Gui"], "Chou": ["Ji", "Gui", "Xin"], "Yin": ["Jia", "Bing", "Wu"],
    "Mao": ["Yi"], "Chen": ["Wu", "Yi", "Gui"], "Si": ["Bing", "Wu", "Geng"],
    "Wu": ["Ding", "Ji"], "Wei": ["Ji", "Ding", "Yi"], "Shen": ["Geng", "Ren", "Wu"],
    "You": ["Xin"], "Xu": ["Wu", "Xin", "Ding"], "Hai": ["Ren", "Jia"],
}

BAZI_FIVE_TIGER = {0: 2, 5: 2, 1: 4, 6: 4, 2: 6, 7: 6, 3: 8, 8: 8, 4: 0, 9: 0}
BAZI_FIVE_RAT = {0: 0, 5: 0, 1: 2, 6: 2, 2: 4, 7: 4, 3: 6, 8: 6, 4: 8, 9: 8}

BAZI_GENERATES = {"Wood": "Fire", "Fire": "Earth", "Earth": "Metal",
                  "Metal": "Water", "Water": "Wood"}
BAZI_CONTROLS = {"Wood": "Earth", "Earth": "Water", "Water": "Fire",
                 "Fire": "Metal", "Metal": "Wood"}

# The 12 "Jie" solar terms defining BaZi month boundaries, 30 degrees
# apart in tropical solar longitude. Branch shown is fixed regardless
# of year (Li Chun always starts the Yin month, etc).
JIE_TERMS = [
    ("Li Chun", 315.0, "Yin"), ("Jing Zhe", 345.0, "Mao"),
    ("Qing Ming", 15.0, "Chen"), ("Li Xia", 45.0, "Si"),
    ("Mang Zhong", 75.0, "Wu"), ("Xiao Shu", 105.0, "Wei"),
    ("Li Qiu", 135.0, "Shen"), ("Bai Lu", 165.0, "You"),
    ("Han Lu", 195.0, "Xu"), ("Li Dong", 225.0, "Hai"),
    ("Da Xue", 255.0, "Zi"), ("Xiao Han", 285.0, "Chou"),
]

# Day-pillar epoch: 1900-01-31 (Gregorian) = Jia-Chen day, a commonly
# used reference in Chinese calendar software. NOT independently
# verified here (no ephemeris/historical-record access in this
# environment) - check this specific anchor against an external BaZi
# calculator before trusting the Day Master and anything derived from
# it (Ten Gods, useful-element analysis).
BAZI_EPOCH_JDN = 2415051
BAZI_EPOCH_STEM = 0
BAZI_EPOCH_BRANCH = 4


def civil_jdn(year: int, month: int, day: int) -> int:
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return (day + (153 * m + 2) // 5 + 365 * y + y // 4
            - y // 100 + y // 400 - 32045)


def sun_longitude(jd: float) -> float:
    res, _ = swe.calc_ut(jd, swe.SUN, swe.FLG_SWIEPH)
    return res[0] % 360.0


def find_solar_term_jd(jd_guess: float, target_lon: float) -> float:
    jd = jd_guess
    for _ in range(50):
        diff = ((target_lon - sun_longitude(jd) + 180.0) % 360.0) - 180.0
        if abs(diff) < 1e-7:
            break
        jd += diff / 0.9856
    return jd


def bazi_stem_branch(idx_stem: int, idx_branch: int) -> dict:
    return {
        "stem": BAZI_STEMS[idx_stem],
        "stem_element": BAZI_STEM_ELEMENT[idx_stem],
        "stem_polarity": "Yang" if BAZI_STEM_YANG[idx_stem] else "Yin",
        "branch": BAZI_BRANCHES[idx_branch],
        "branch_element": BAZI_BRANCH_ELEMENT[idx_branch],
        "hidden_stems": BAZI_HIDDEN_STEMS[BAZI_BRANCHES[idx_branch]],
    }


def bazi_ten_god(day_stem_idx: int, other_stem_idx: int) -> str:
    de, dy = BAZI_STEM_ELEMENT[day_stem_idx], BAZI_STEM_YANG[day_stem_idx]
    oe, oy = BAZI_STEM_ELEMENT[other_stem_idx], BAZI_STEM_YANG[other_stem_idx]
    same = (dy == oy)
    if oe == de:
        return "Friend" if same else "Rob Wealth"
    if BAZI_GENERATES[de] == oe:
        return "Eating God" if same else "Hurting Officer"
    if BAZI_CONTROLS[de] == oe:
        return "Indirect Wealth" if same else "Direct Wealth"
    if BAZI_CONTROLS[oe] == de:
        return "Seven Killings" if same else "Direct Officer"
    if BAZI_GENERATES[oe] == de:
        return "Indirect Seal" if same else "Direct Seal"
    return "Unknown"


class BaziRequest(BirthDataCoords):
    gender: str = Field(default="male", pattern="^(male|female)$")


@app.post("/bazi_chart", dependencies=[Depends(verify_api_key)])
@with_engine("bazi")
def bazi_chart(data: BaziRequest):
    """Calculate a BaZi (Four Pillars) chart: year/month/day/hour
    stems and branches, hidden stems, Ten Gods vs the Day Master, and
    Luck Pillars. The four natal pillars matched two independent
    calendar libraries; luck-pillar start age is convention-dependent
    (about weeks), the pillar sequence is not."""
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

        # --- Year pillar (boundary = Li Chun of the Gregorian year) ---
        li_chun_guess = swe.julday(data.year, 2, 4, 12.0)
        li_chun_jd = find_solar_term_jd(li_chun_guess, 315.0)
        bazi_year = data.year if jd >= li_chun_jd else data.year - 1
        year_stem_idx = (bazi_year - 4) % 10
        year_branch_idx = (bazi_year - 4) % 12

        # --- Month pillar: bracket birth between consecutive Jie terms ---
        this_year_terms = [
            (name, branch, find_solar_term_jd(
                swe.julday(data.year, 2, 4, 12.0), lon))
            for name, lon, branch in JIE_TERMS
        ]
        prev_year_terms = [
            (name, branch, find_solar_term_jd(
                swe.julday(data.year - 1, 2, 4, 12.0), lon))
            for name, lon, branch in JIE_TERMS
        ]
        all_terms = sorted(this_year_terms + prev_year_terms,
                           key=lambda t: t[2])

        month_branch = None
        prev_term_jd = next_term_jd = None
        for i in range(len(all_terms) - 1):
            if all_terms[i][2] <= jd < all_terms[i + 1][2]:
                month_branch = all_terms[i][1]
                prev_term_jd = all_terms[i][2]
                next_term_jd = all_terms[i + 1][2]
                break
        if month_branch is None:
            raise RuntimeError("could not bracket birth date between solar terms")

        month_branch_idx = BAZI_BRANCHES.index(month_branch)
        month_offset = (month_branch_idx - 2) % 12
        month_stem_idx = (BAZI_FIVE_TIGER[year_stem_idx] + month_offset) % 10

        # --- Day pillar: continuous 60-cycle from the reference epoch ---
        day_jdn = civil_jdn(local.year, local.month, local.day)
        offset = day_jdn - BAZI_EPOCH_JDN
        day_stem_idx = (BAZI_EPOCH_STEM + offset) % 10
        day_branch_idx = (BAZI_EPOCH_BRANCH + offset) % 12

        # --- Hour pillar ---
        h = local.hour
        hour_branch_idx = ((h + 1) // 2) % 12
        hour_stem_idx = (BAZI_FIVE_RAT[day_stem_idx] + hour_branch_idx) % 10

        pillars = {
            "year": bazi_stem_branch(year_stem_idx, year_branch_idx),
            "month": bazi_stem_branch(month_stem_idx, month_branch_idx),
            "day": bazi_stem_branch(day_stem_idx, day_branch_idx),
            "hour": bazi_stem_branch(hour_stem_idx, hour_branch_idx),
        }
        for key, p in pillars.items():
            stem_idx = BAZI_STEMS.index(p["stem"])
            p["ten_god_of_stem"] = ("Day Master" if key == "day"
                                    else bazi_ten_god(day_stem_idx, stem_idx))
            p["ten_gods_of_hidden_stems"] = [
                bazi_ten_god(day_stem_idx, BAZI_STEMS.index(hs))
                for hs in p["hidden_stems"]
            ]

        # --- Luck Pillars (Da Yun), 10-year periods ---
        year_is_yang = BAZI_STEM_YANG[year_stem_idx]
        male = (data.gender == "male")
        forward = (year_is_yang and male) or (not year_is_yang and not male)
        days_to_boundary = (next_term_jd - jd) if forward else (jd - prev_term_jd)
        start_age_years = days_to_boundary / 3.0

        luck_pillars = []
        cur_stem, cur_branch = month_stem_idx, month_branch_idx
        for i in range(8):
            if forward:
                cur_stem = (cur_stem + 1) % 10
                cur_branch = (cur_branch + 1) % 12
            else:
                cur_stem = (cur_stem - 1) % 10
                cur_branch = (cur_branch - 1) % 12
            entry = bazi_stem_branch(cur_stem, cur_branch)
            entry["start_age"] = round(start_age_years + i * 10, 2)
            entry["ten_god_of_stem"] = bazi_ten_god(day_stem_idx, cur_stem)
            luck_pillars.append(entry)

        return {
            "verification_note": (
                "Verified: all four natal pillars (including the day "
                "pillar epoch 1900-01-31 = Jia-Chen) matched the "
                "lunar-python and lunar-javascript calendar libraries "
                "on an independent test chart. Luck-pillar START AGE "
                "uses continuous 3 days = 1 year; libraries that round "
                "to days and two-hour units can differ by a few weeks. "
                "The luck-pillar SEQUENCE does not depend on this. "
                "Day boundary is "
                "assumed at local midnight; some schools use 23:00 "
                "(late Zi) instead - a known disagreement between "
                "schools, not a bug."
            ),
            "confidence": {
                "natal_pillars": "verified against two calendar libraries",
                "luck_pillar_sequence": "verified rule",
                "luck_start_age": "convention-dependent, about weeks",
            },
            "day_master": BAZI_STEMS[day_stem_idx],
            "day_master_element": BAZI_STEM_ELEMENT[day_stem_idx],
            "day_master_polarity": "Yang" if BAZI_STEM_YANG[day_stem_idx] else "Yin",
            "pillars": pillars,
            "luck_pillars": luck_pillars,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"BaZi calculation error: {e}")


# =====================================================
# Zi Wei Dou Shu (Purple Star Astrology)
# =====================================================
# Depends on the Chinese lunisolar calendar (computed here from
# ephemeris new moons + zhongqi solar terms) and on classical
# placement tables. Verification hierarchy is described in the
# endpoint's verification_note.

NAYIN_ELEMENTS = [
    "Metal", "Fire", "Wood", "Earth", "Metal",
    "Fire", "Water", "Earth", "Metal", "Wood",
    "Water", "Earth", "Fire", "Wood", "Water",
    "Metal", "Fire", "Wood", "Earth", "Metal",
    "Fire", "Water", "Earth", "Metal", "Wood",
    "Water", "Earth", "Fire", "Wood", "Water",
]
BUREAU_FROM_ELEMENT = {"Water": 2, "Wood": 3, "Metal": 4, "Earth": 5, "Fire": 6}
BUREAU_NAMES = {2: "Water 2", 3: "Wood 3", 4: "Metal 4", 5: "Earth 5", 6: "Fire 6"}

ZIWEI_SERIES = {"TianJi": -1, "TaiYang": -3, "WuQu": -4, "TianTong": -5,
                "LianZhen": -8}
TIANFU_SERIES = {"TaiYin": 1, "TanLang": 2, "JuMen": 3, "TianXiang": 4,
                 "TianLiang": 5, "QiSha": 6, "PoJun": 10}

PALACE_NAMES = ["Ming (Life)", "Siblings", "Spouse", "Children", "Wealth",
                "Health", "Travel", "Friends", "Career", "Property",
                "Fortune (Fu De)", "Parents"]

# Si Hua (Four Transformations) by year stem: (Lu, Quan, Ke, Ji).
# The San He mainstream table; stems Wu, Geng and Ren are DISPUTED
# between schools - flagged in the response.
SIHUA_TABLE = {
    "Jia": ("LianZhen", "PoJun", "WuQu", "TaiYang"),
    "Yi": ("TianJi", "TianLiang", "ZiWei", "TaiYin"),
    "Bing": ("TianTong", "TianJi", "WenChang", "LianZhen"),
    "Ding": ("TaiYin", "TianTong", "TianJi", "JuMen"),
    "Wu": ("TanLang", "TaiYin", "YouBi", "TianJi"),
    "Ji": ("WuQu", "TanLang", "TianLiang", "WenQu"),
    "Geng": ("TaiYang", "WuQu", "TaiYin", "TianTong"),
    "Xin": ("JuMen", "TaiYang", "WenQu", "WenChang"),
    "Ren": ("TianLiang", "ZiWei", "ZuoFu", "WuQu"),
    "Gui": ("PoJun", "JuMen", "TaiYin", "TanLang"),
}
SIHUA_DISPUTED_STEMS = ["Wu", "Geng", "Ren", "Xin"]


def moon_longitude(jd: float) -> float:
    res, _ = swe.calc_ut(jd, swe.MOON, swe.FLG_SWIEPH)
    return res[0] % 360.0


def elongation(jd: float) -> float:
    return (moon_longitude(jd) - sun_longitude(jd)) % 360.0


def refine_new_moon(jd_guess: float) -> float:
    """Newton-iterate to the new moon nearest the guess."""
    jd = jd_guess
    for _ in range(60):
        e = elongation(jd)
        diff = ((e + 180.0) % 360.0) - 180.0
        if abs(diff) < 1e-7:
            break
        jd -= diff / 12.1907
    return jd


def prev_new_moon(jd: float) -> float:
    nm = refine_new_moon(jd)
    while nm > jd + 1e-9:
        nm = refine_new_moon(nm - 29.530588)
    return nm


def jd_to_local_date(jd: float, tz: ZoneInfo):
    y, m, d, h = swe.revjul(jd)
    # timedelta carries the fractional hour at full precision - no
    # truncation that could shift the date near midnight
    dt = (datetime(y, m, d, tzinfo=ZoneInfo("UTC"))
          + timedelta(hours=float(h)))
    return dt.astimezone(tz).date()


def zhongqi_crossed(jd_start: float, jd_end: float):
    """Which multiples of 30 deg of solar longitude the Sun crosses
    in (jd_start, jd_end]. Returns list of longitudes."""
    lon0 = sun_longitude(jd_start)
    lon1 = sun_longitude(jd_end)
    span = (lon1 - lon0) % 360.0
    crossed = []
    k = (int(lon0 // 30) + 1) * 30
    while ((k - lon0) % 360.0) <= span:
        crossed.append(k % 360)
        k += 30
        if len(crossed) > 3:
            break
    return crossed


def month_from_zhongqi_lon(lon: float) -> int:
    return ((int(round(lon)) - 330) // 30) % 12 + 1


def compute_lunar_date(jd_birth: float, birth_year: int, tz: ZoneInfo,
                       day_boundary: str = "local"):
    """Chinese lunisolar month/day for the birth moment.
    Month numbering anchored at the winter-solstice lunation (month
    11); a lunation with no zhongqi is a leap month (repeats the
    previous number). Adequate for 20th-21st century dates.
    day_boundary: "local" (default) or "beijing" - which midnight
    defines the calendar day (a known school difference)."""
    ws_guess = swe.julday(birth_year, 12, 21, 12.0)
    ws = find_solar_term_jd(ws_guess, 270.0)
    if ws > jd_birth:
        ws_guess = swe.julday(birth_year - 1, 12, 21, 12.0)
        ws = find_solar_term_jd(ws_guess, 270.0)
    ws_year = swe.revjul(ws)[0]

    # Precompute the new-moon chain ONCE: from the month-11 lunation
    # up to past the birth. 15 lunations always covers a solstice-to-
    # birth span (max ~13 lunations in a 13-month sui) with margin.
    moons = [prev_new_moon(ws)]
    for _ in range(15):
        moons.append(refine_new_moon(moons[-1] + 29.530588))
        if moons[-1] > jd_birth:
            break

    if jd_birth < moons[0] - 1e-6:
        raise RuntimeError(
            "internal inconsistency: birth precedes the month-11 "
            "lunation of its own sui - please report this date")

    # Walk the chain, numbering each lunation by its zhongqi
    month_num, is_leap = 11, False
    birth_idx = None
    for i in range(len(moons) - 1):
        if i > 0:
            crossed = zhongqi_crossed(moons[i], moons[i + 1])
            if not crossed:
                is_leap = True          # leap: repeats previous number
            else:
                is_leap = False
                month_num = month_from_zhongqi_lon(crossed[0])
        if moons[i] - 1e-9 <= jd_birth < moons[i + 1] - 1e-9:
            birth_idx = i
            break
    if birth_idx is None:
        raise RuntimeError("could not locate birth lunation")

    day_tz = tz if day_boundary == "local" else ZoneInfo("Asia/Shanghai")
    lunar_day = (jd_to_local_date(jd_birth, day_tz)
                 - jd_to_local_date(moons[birth_idx], day_tz)).days + 1
    lunar_year = ws_year if month_num in (11, 12) else ws_year + 1
    return month_num, is_leap, lunar_day, lunar_year


def ziwei_position(bureau: int, day: int) -> int:
    n = -(-day // bureau)           # ceil
    r = n * bureau - day
    base = 2 + (n - 1)
    if r == 0:
        pos = base
    elif r % 2 == 1:
        pos = base - r
    else:
        pos = base + r
    return pos % 12


class ZiweiRequest(BirthDataCoords):
    gender: str = Field(default="male", pattern="^(male|female)$")
    day_boundary: str = Field(default="local", pattern="^(local|beijing)$")


@app.post("/ziwei_chart", dependencies=[Depends(verify_api_key)])
@with_engine("ziwei")
def ziwei_chart(data: ZiweiRequest):
    """Calculate a Zi Wei Dou Shu chart: lunisolar date, Ming/Shen
    palaces, Five-Element Bureau, 14 major stars, Chang/Qu/Fu/Bi,
    Four Transformations and decade periods. New module with layered
    confidence - verify the lunar date and Ming palace first."""
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

        month_num, is_leap, lunar_day, lunar_year = compute_lunar_date(
            jd, local.year, tz, data.day_boundary)

        # Leap-month convention (school-dependent): first 15 days
        # belong to the same month number, the rest to the next.
        eff_month = month_num
        if is_leap and lunar_day > 15:
            eff_month = month_num % 12 + 1

        hour_idx = ((local.hour + 1) // 2) % 12
        year_stem_idx = (lunar_year - 4) % 10
        year_branch_idx = (lunar_year - 4) % 12

        ming = (2 + (eff_month - 1) - hour_idx) % 12
        shen = (2 + (eff_month - 1) + hour_idx) % 12

        # palace stems via Five Tiger rule from the (lunar) year stem
        ft = BAZI_FIVE_TIGER[year_stem_idx]
        def palace_stem(branch_idx: int) -> int:
            return (ft + ((branch_idx - 2) % 12)) % 10

        ming_stem_idx = palace_stem(ming)
        pair_idx = next(i for i in range(60)
                        if i % 10 == ming_stem_idx and i % 12 == ming)
        element = NAYIN_ELEMENTS[pair_idx // 2]
        bureau = BUREAU_FROM_ELEMENT[element]

        zw = ziwei_position(bureau, lunar_day)
        tf = (4 - zw) % 12

        star_positions = {"ZiWei": zw, "TianFu": tf}
        for star, off in ZIWEI_SERIES.items():
            star_positions[star] = (zw + off) % 12
        for star, off in TIANFU_SERIES.items():
            star_positions[star] = (tf + off) % 12
        # auxiliary stars needed for Si Hua completeness
        star_positions["WenChang"] = (10 - hour_idx) % 12
        star_positions["WenQu"] = (4 + hour_idx) % 12
        star_positions["ZuoFu"] = (4 + (eff_month - 1)) % 12
        star_positions["YouBi"] = (10 - (eff_month - 1)) % 12

        year_stem_name = BAZI_STEMS[year_stem_idx]
        lu, quan, ke, ji = SIHUA_TABLE[year_stem_name]

        palaces = []
        for k in range(12):
            b = (ming - k) % 12
            stars_here = sorted(s for s, p in star_positions.items() if p == b)
            palaces.append({
                "palace": PALACE_NAMES[k],
                "branch": BAZI_BRANCHES[b],
                "stem": BAZI_STEMS[palace_stem(b)],
                "stars": stars_here,
                "is_shen_palace": (b == shen),
            })

        year_is_yang = BAZI_STEM_YANG[year_stem_idx]
        male = (data.gender == "male")
        forward = (year_is_yang and male) or (not year_is_yang and not male)
        decades = []
        for i in range(8):
            b = (ming + i) % 12 if forward else (ming - i) % 12
            decades.append({
                "start_age": bureau + i * 10,
                "end_age": bureau + i * 10 + 9,
                "palace_branch": BAZI_BRANCHES[b],
            })

        return {
            "verification_note": (
                "New module with LAYERED confidence - verify in this "
                "order: (1) lunar_info month/day against any Chinese "
                "lunar calendar converter - this is pure ephemeris "
                "and fully checkable; (2) Ming palace branch and "
                "Bureau against a ZWDS calculator; (3) ZiWei star "
                "position (formula reproduced all 5 textbook day-1 "
                "anchors in offline tests); (4) Si Hua last - stems "
                "Wu, Geng, Ren are genuinely DISPUTED between "
                "schools, a mismatch there may be a school "
                "difference rather than a bug. Conventions used: "
                "local-midnight day boundary; leap-month day 1-15 = "
                "same month, 16+ = next; year boundary = lunar new "
                "year (NOT Li Chun - differs from BaZi on purpose). "
                "Minor stars (Lu Cun, Huo/Ling, Qing Yang/Tuo Luo, "
                "Tian Ma etc.) and brightness levels are NOT "
                "computed - never invent them; state they are "
                "not available."
            ),
            "confidence": {
                "lunar_calendar": "ephemeris-based - checkable",
                "ming_shen_bureau": "classical formulas, unverified externally",
                "star_positions": "formula passed 5 textbook anchors",
                "si_hua": "school-dependent for stems Wu/Geng/Ren",
            },
            "lunar_info": {
                "day_boundary_used": data.day_boundary,
                "lunar_year": lunar_year,
                "lunar_month": month_num,
                "is_leap_month": is_leap,
                "effective_month_used": eff_month,
                "lunar_day": lunar_day,
                "year_pillar": f"{year_stem_name} {BAZI_BRANCHES[year_branch_idx]}",
            },
            "ming_palace_branch": BAZI_BRANCHES[ming],
            "shen_palace_branch": BAZI_BRANCHES[shen],
            "bureau": BUREAU_NAMES[bureau],
            "four_transformations": {
                "year_stem": year_stem_name,
                "hua_lu": lu, "hua_quan": quan, "hua_ke": ke, "hua_ji": ji,
                "school_note": ("DISPUTED between schools for this stem"
                                if year_stem_name in SIHUA_DISPUTED_STEMS
                                else "mainstream San He table"),
            },
            "palaces": palaces,
            "decade_periods": decades,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"Zi Wei Dou Shu calculation error: {e}")


# =====================================================
# Vedic D7 Saptamsha (children / progeny divisional chart)
# =====================================================
# Pure arithmetic on top of the already-verified sidereal layer:
# each sign is split into 7 parts of 30/7 degrees; odd signs count
# the parts from the sign itself, even signs from the 7th sign
# (classical Parashara rule).

D7_SPAN = 30.0 / 7.0


def d7_sign_index(sid_lon: float) -> int:
    s = int(sid_lon // 30) % 12
    d = sid_lon % 30
    part = int(d // D7_SPAN)
    if part > 6:
        part = 6                    # guard the exact 30.0 edge
    odd_sign = (s % 2 == 0)         # index 0 = Aries = 1st (odd) sign
    start = s if odd_sign else (s + 6) % 12
    return (start + part) % 12


@app.post("/vedic_d7", dependencies=[Depends(verify_api_key)])
@with_engine("vedic")
def vedic_d7(data: BirthDataCoords):
    """Calculate the D7 Saptamsha divisional chart (children/progeny)
    from the same verified sidereal engine as /vedic_chart: D7 sign
    for the Ascendant and all planets plus Rahu/Ketu, with whole-sign
    houses from the D7 Lagna. Requires coordinates and timezone."""
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
        d7_lagna_idx = d7_sign_index(asc_sid)

        def entry(sid_lon: float) -> dict:
            idx = d7_sign_index(sid_lon)
            return {
                "d1_sidereal_longitude": round(sid_lon, 3),
                "d7_sign": RASHIS[idx],
                "d7_house_from_lagna": ((idx - d7_lagna_idx) % 12) + 1,
            }

        planets = {}
        for key, body in VEDIC_BODIES:
            trop, speed = tropical_lon_and_speed(jd, body)
            sid = (trop - ayan) % 360.0
            planets[key] = entry(sid)
            planets[key]["retrograde"] = speed < 0

        true_node_trop, _ = tropical_lon_and_speed(jd, swe.TRUE_NODE)
        mean_node_trop, _ = tropical_lon_and_speed(jd, swe.MEAN_NODE)
        for label, trop in (("rahu_true_node", true_node_trop),
                            ("rahu_mean_node", mean_node_trop)):
            sid = (trop - ayan) % 360.0
            planets[label] = entry(sid)
            planets[label.replace("rahu", "ketu")] = entry((sid + 180.0) % 360.0)

        return {
            "verification_note": (
                "D7 is pure arithmetic on the sidereal layer already "
                "verified against Prokerala (ayanamsha, Moon nakshatra "
                "and sign matched). The Parashara counting rule (odd "
                "signs from self, even from the 7th) passed anchor and "
                "boundary tests offline. Spot-check the D7 Lagna "
                "against a Jyotish calculator that shows Saptamsha "
                "for extra confidence. Interpretation of D7 houses "
                "(children, creativity, lineage) is the symbolic "
                "layer - not part of this calculation."
            ),
            "confidence": {
                "level": "high (arithmetic over verified sidereal positions)",
            },
            "ayanamsha": {"mode": "Lahiri", "value_deg": round(ayan, 5)},
            "d7_lagna": {
                "d1_ascendant_sidereal": round(asc_sid, 3),
                "d7_sign": RASHIS[d7_lagna_idx],
            },
            "houses_whole_sign": {
                str(h + 1): RASHIS[(d7_lagna_idx + h) % 12] for h in range(12)
            },
            "planets": planets,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"D7 Saptamsha calculation error: {e}")


# =====================================================
# Numerology (Pythagorean), Destiny Matrix, Tarot birth cards
# =====================================================
# Pure arithmetic on the birth date (and birth name for numerology).
# Conventions that differ between schools are reported explicitly.

NUM_MASTER = (11, 22, 33)
NUM_LATIN = {ch: (i % 9) + 1 for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
NUM_CYRILLIC = {ch: (i % 9) + 1
                for i, ch in enumerate("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")}
NUM_VOWELS = set("AEIOU") | set("АЕЁИОУЫЭЮЯ")
NUM_KARMIC_DEBT = (13, 14, 16, 19)

ARCANA_RWS = ["The Fool", "The Magician", "The High Priestess", "The Empress",
              "The Emperor", "The Hierophant", "The Lovers", "The Chariot",
              "Strength", "The Hermit", "Wheel of Fortune", "Justice",
              "The Hanged Man", "Death", "Temperance", "The Devil", "The Tower",
              "The Star", "The Moon", "The Sun", "Judgement", "The World"]
# Destiny Matrix uses the Marseille order (8 = Justice, 11 = Strength)
# and numbers the Fool as 22.
ARCANA_MATRIX = {i: ARCANA_RWS[i] for i in range(1, 22)}
ARCANA_MATRIX[8], ARCANA_MATRIX[11] = "Justice", "Strength"
ARCANA_MATRIX[22] = "The Fool"


def num_digit_sum(n: int) -> int:
    return sum(int(c) for c in str(abs(n)))


def num_reduce(n: int, keep_master: bool = True) -> int:
    while n > 9 and not (keep_master and n in NUM_MASTER):
        n = num_digit_sum(n)
    return n


def num_name_values(name: str):
    values, vowels, consonants, ignored = [], [], [], []
    for ch in name.upper():
        if ch in NUM_LATIN or ch in NUM_CYRILLIC:
            v = NUM_LATIN.get(ch) or NUM_CYRILLIC.get(ch)
            values.append(v)
            (vowels if ch in NUM_VOWELS else consonants).append(v)
        elif not ch.isspace() and ch not in "-'.":
            ignored.append(ch)
    return values, vowels, consonants, sorted(set(ignored))


def num_with_trace(total: int, keep_master: bool = True) -> dict:
    return {"number": num_reduce(total, keep_master), "unreduced_total": total,
            "karmic_debt": total if total in NUM_KARMIC_DEBT else None}


class DateRequest(BaseModel):
    year: int = Field(ge=1000, le=2100)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)


class NumerologyRequest(DateRequest):
    full_name: str = ""
    target_date: Optional[str] = Field(default=None,
                                       pattern=r"^\d{4}-\d{2}-\d{2}$")


@app.post("/numerology", dependencies=[Depends(verify_api_key)])
@with_engine("numerology")
def numerology(data: NumerologyRequest):
    """Pythagorean numerology: Life Path (two methods), Birthday,
    Expression, Soul Urge, Personality, Maturity, Pinnacles and
    Challenges, and Personal Year/Month/Day for target_date. Name
    numbers need full_name (Latin or Russian Cyrillic letters)."""
    try:
        datetime(data.year, data.month, data.day)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date: {e}")

    m, d, y = data.month, data.day, data.year
    rm, rd, ry = num_reduce(m), num_reduce(d), num_reduce(num_digit_sum(y))
    life_components = rm + rd + ry
    life_total_digits = num_digit_sum(int(f"{y:04d}{m:02d}{d:02d}"))
    life_path = num_with_trace(life_components)

    single = lambda n: num_reduce(n, keep_master=False)
    sm, sd, sy = single(m), single(d), single(num_digit_sum(y))
    p1 = num_reduce(sm + sd)
    p2 = num_reduce(sd + sy)
    p3 = num_reduce(p1 + p2)
    p4 = num_reduce(sm + sy)
    first_end = 36 - single(life_path["number"])
    pinnacles = [
        {"pinnacle": 1, "number": p1, "age_from": 0, "age_to": first_end},
        {"pinnacle": 2, "number": p2, "age_from": first_end + 1, "age_to": first_end + 9},
        {"pinnacle": 3, "number": p3, "age_from": first_end + 10, "age_to": first_end + 18},
        {"pinnacle": 4, "number": p4, "age_from": first_end + 19, "age_to": None},
    ]
    c1, c2 = abs(sm - sd), abs(sd - sy)
    challenges = [abs(sm - sd), abs(sd - sy), abs(c1 - c2), abs(sm - sy)]

    result = {
        "verification_note": (
            "Pure arithmetic. Life Path is given by two common methods "
            "(reduce month/day/year separately vs. sum all digits); "
            "they can differ, especially for master numbers. Master "
            "numbers 11/22/33 are kept in core numbers; personal "
            "cycles are reduced to 1-9. Letter values: standard "
            "Pythagorean table (A=1..I=9) and the sequential Russian "
            "table (А=1..Я=6). Y is treated as a consonant."
        ),
        "confidence": {"level": "exact arithmetic; method conventions noted"},
        "life_path": {
            "number": life_path["number"],
            "method": "components (month, day, year reduced separately)",
            "components": {"month": rm, "day": rd, "year": ry},
            "unreduced_total": life_components,
            "karmic_debt": life_path["karmic_debt"],
            "alternative_all_digits_method": num_reduce(life_total_digits),
        },
        "birthday_number": num_reduce(d),
        "pinnacles": pinnacles,
        "challenges": [{"challenge": i + 1, "number": c}
                       for i, c in enumerate(challenges)],
    }

    if data.full_name.strip():
        values, vowels, consonants, ignored = num_name_values(data.full_name)
        if not values:
            raise HTTPException(status_code=422, detail=(
                "full_name contains no Latin or Russian letters"))
        expression = num_with_trace(sum(values))
        result["name_numbers"] = {
            "full_name_used": data.full_name.strip(),
            "expression_destiny": expression,
            "soul_urge_heart": num_with_trace(sum(vowels)) if vowels else None,
            "personality": num_with_trace(sum(consonants)) if consonants else None,
            "maturity": num_reduce(life_path["number"] + expression["number"]),
            "ignored_characters": ignored,
        }
    else:
        result["name_numbers"] = None

    if data.target_date:
        ty, tm, td = (int(x) for x in data.target_date.split("-"))
        personal_year = single(sm + sd + single(num_digit_sum(ty)))
        personal_month = single(personal_year + tm)
        personal_day = single(personal_month + td)
        result["personal_cycles"] = {
            "target_date": data.target_date,
            "personal_year": personal_year,
            "personal_month": personal_month,
            "personal_day": personal_day,
        }
    return result


def dm_reduce(n: int) -> int:
    while n > 22:
        n = num_digit_sum(n)
    return n


def dm_point(n: int) -> dict:
    return {"arcanum": n, "name": ARCANA_MATRIX[n]}


@app.post("/destiny_matrix", dependencies=[Depends(verify_api_key)])
@with_engine("destiny_matrix")
def destiny_matrix(data: DateRequest):
    """Destiny Matrix (22 arcana method): the four outer points, centre,
    ancestral square and the personal, social and spiritual purposes.
    Only core points that the main schools agree on are returned."""
    try:
        datetime(data.year, data.month, data.day)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date: {e}")
    A = dm_reduce(data.day)
    B = dm_reduce(data.month)
    C = dm_reduce(num_digit_sum(data.year))
    D = dm_reduce(A + B + C)
    E = dm_reduce(A + B + C + D)
    F, G, H, I = dm_reduce(A + B), dm_reduce(B + C), dm_reduce(C + D), dm_reduce(D + A)
    sky, earth = dm_reduce(B + D), dm_reduce(A + C)
    personal = dm_reduce(sky + earth)
    male, female = dm_reduce(F + H), dm_reduce(G + I)
    social = dm_reduce(male + female)
    spiritual = dm_reduce(personal + social)
    return {
        "verification_note": (
            "Matched three published worked examples. Pure "
            "arithmetic: numbers above 22 are reduced by digit sum. "
            "Arcana numbering follows the Matrix convention (8 = "
            "Justice, 11 = Strength, 22 = The Fool). Secondary points "
            "(money and love channels, karmic tail details, health "
            "map) differ between schools and are not computed."
        ),
        "confidence": {"level": "verified against published examples"},
        "outer_points": {
            "A_day_left": dm_point(A), "B_month_top": dm_point(B),
            "C_year_right": dm_point(C), "D_bottom": dm_point(D),
        },
        "center_E_comfort_zone": dm_point(E),
        "ancestral_square": {
            "F_top_left": dm_point(F), "G_top_right": dm_point(G),
            "H_bottom_right": dm_point(H), "I_bottom_left": dm_point(I),
        },
        "purposes": {
            "sky_line": dm_point(sky), "earth_line": dm_point(earth),
            "personal_purpose": dm_point(personal),
            "male_line": dm_point(male), "female_line": dm_point(female),
            "social_purpose": dm_point(social),
            "spiritual_purpose": dm_point(spiritual),
        },
    }


@app.post("/tarot_birth_cards", dependencies=[Depends(verify_api_key)])
@with_engine("tarot_birth_cards")
def tarot_birth_cards(data: DateRequest):
    """Tarot birth cards (Mary K. Greer method): month + day + year,
    reduced to 22 or less, gives the Personality card and its digit
    sum the Soul card. Rider-Waite numbering (8 Strength, 11 Justice)."""
    try:
        datetime(data.year, data.month, data.day)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid date: {e}")
    total = data.month + data.day + data.year
    n = total
    while n > 22:
        n = num_digit_sum(n)
    if n == 22:
        chain = [0, 4]
    else:
        chain = [n]
        while chain[-1] > 9:
            chain.append(num_digit_sum(chain[-1]))
    cards = [{"number": c, "name": ARCANA_RWS[c]} for c in chain]
    return {
        "verification_note": (
            "Pure arithmetic (Greer method). A total of 22 maps to The "
            "Fool with The Emperor (4); 19 gives a triple Sun-Wheel-"
            "Magician chain. Single-digit totals give one card."
        ),
        "confidence": {"level": "exact arithmetic"},
        "date_sum": total,
        "personality_card": cards[0],
        "soul_card": cards[-1],
        "full_chain": cards,
    }


# =====================================================
# Feng Shui: Kua number (Eight Mansions), Nine Star Ki, flying stars
# =====================================================

FS_DIRECTIONS = {  # Kua -> 4 favourable then 4 unfavourable directions
    1: ("SE", "E", "S", "N", "W", "NE", "NW", "SW"),
    2: ("NE", "W", "NW", "SW", "E", "SE", "S", "N"),
    3: ("S", "N", "SE", "E", "SW", "NW", "NE", "W"),
    4: ("N", "S", "E", "SE", "NW", "SW", "W", "NE"),
    6: ("W", "NE", "SW", "NW", "SE", "E", "N", "S"),
    7: ("NW", "SW", "NE", "W", "N", "S", "SE", "E"),
    8: ("SW", "NW", "W", "NE", "S", "N", "E", "SE"),
    9: ("E", "SE", "N", "S", "NE", "W", "SW", "NW"),
}
FS_DIRECTION_NAMES = ["Sheng Qi (success)", "Tian Yi (health)",
                      "Yan Nian (relationships)", "Fu Wei (stability)",
                      "Huo Hai (mishaps)", "Wu Gui (five ghosts)",
                      "Liu Sha (six killings)", "Jue Ming (total loss)"]
FS_STAR_NAMES = {1: "1 White Water", 2: "2 Black Earth", 3: "3 Jade Wood",
                 4: "4 Green Wood", 5: "5 Yellow Earth", 6: "6 White Metal",
                 7: "7 Red Metal", 8: "8 White Earth", 9: "9 Purple Fire"}
# Lo Shu flight path of the stars, starting from the centre
FS_FLIGHT = ["Center", "NW", "W", "NE", "S", "N", "SW", "E", "SE"]


def fs_single(n: int) -> int:
    while n > 9:
        n = num_digit_sum(n)
    return n


def fs_year_star(year: int) -> int:
    star = 11 - fs_single(num_digit_sum(year))
    while star > 9:
        star -= 9
    return star


def fs_flying_chart(center: int) -> dict:
    return {pos: FS_STAR_NAMES[((center - 1 + k) % 9) + 1]
            for k, pos in enumerate(FS_FLIGHT)}


def fs_period(year: int) -> int:
    return ((year - 1864) // 20) % 9 + 1


class FengShuiRequest(BirthDataCoords):
    gender: str = Field(default="male", pattern="^(male|female)$")
    target_year: Optional[int] = Field(default=None, ge=1900, le=2100)


@app.post("/feng_shui", dependencies=[Depends(verify_api_key)])
@with_engine("feng_shui")
def feng_shui(data: FengShuiRequest):
    """Personal Feng Shui: Kua number with its 4 favourable and 4
    unfavourable directions (Eight Mansions), Nine Star Ki year and
    month stars, and the annual flying-star chart for target_year.
    The solar year starts at Li Chun (Sun at 315 deg), not Jan 1."""
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
        sun_lon = sun_longitude(jd)
        fs_year = data.year
        if data.month <= 2 and 270.0 <= sun_lon < 315.0:
            fs_year -= 1

        year_star = fs_year_star(fs_year)
        s = fs_single(num_digit_sum(fs_year))
        if data.gender == "male":
            kua = 2 if year_star == 5 else year_star
        else:
            kua = fs_single(s + 4)
            kua = 8 if kua == 5 else kua
        dirs = FS_DIRECTIONS[kua]
        group = "East" if kua in (1, 3, 4, 9) else "West"

        month_index = int(((sun_lon - 315.0) % 360.0) // 30)  # 0 = Yin month
        start = {1: 8, 4: 8, 7: 8, 3: 5, 6: 5, 9: 5, 2: 2, 5: 2, 8: 2}[year_star]
        month_star = ((start - 1 - month_index) % 9) + 1

        if data.target_year:
            target_year = data.target_year
        else:
            now = datetime.now(ZoneInfo("UTC"))
            jd_now = swe.julday(now.year, now.month, now.day,
                                now.hour + now.minute / 60)
            target_year = now.year
            if now.month <= 2 and 270.0 <= sun_longitude(jd_now) < 315.0:
                target_year -= 1          # before Li Chun: previous solar year
        annual_star = fs_year_star(target_year)

        return {
            "verification_note": (
                "Kua and Nine Star Ki use the Li Chun year boundary "
                "computed from the Sun's longitude. Kua 5 is replaced by "
                "2 (male) / 8 (female), the mainstream Eight Mansions "
                "rule. A house chart (Xuan Kong natal chart of a "
                "building) needs the facing direction and construction "
                "period and is not computed."
            ),
            "confidence": {"level": "exact arithmetic over solar-term year"},
            "feng_shui_birth_year": fs_year,
            "kua": {
                "number": kua, "group": group,
                "favourable": {FS_DIRECTION_NAMES[i]: dirs[i] for i in range(4)},
                "unfavourable": {FS_DIRECTION_NAMES[i]: dirs[i] for i in range(4, 8)},
            },
            "nine_star_ki": {
                "year_star": FS_STAR_NAMES[year_star],
                "month_star": FS_STAR_NAMES[month_star],
                "natal_year_chart": fs_flying_chart(year_star),
            },
            "annual_flying_stars": {
                "year": target_year,
                "center_star": FS_STAR_NAMES[annual_star],
                "period": fs_period(target_year),
                "chart": fs_flying_chart(annual_star),
                "note": "Annual chart changes at Li Chun (about Feb 4).",
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Feng Shui calculation error: {e}")


# =====================================================
# I Ching: birth hexagram by the Plum Blossom (Mei Hua) time method
# =====================================================

IC_TRIGRAMS = {  # Early Heaven number: (name, lines bottom->top)
    1: ("Qian (Heaven)", (1, 1, 1)), 2: ("Dui (Lake)", (1, 1, 0)),
    3: ("Li (Fire)", (1, 0, 1)), 4: ("Zhen (Thunder)", (1, 0, 0)),
    5: ("Xun (Wind)", (0, 1, 1)), 6: ("Kan (Water)", (0, 1, 0)),
    7: ("Gen (Mountain)", (0, 0, 1)), 8: ("Kun (Earth)", (0, 0, 0)),
}
IC_LINES_TO_TRIGRAM = {v[1]: k for k, v in IC_TRIGRAMS.items()}
# King Wen number by (upper, lower) Early Heaven trigram numbers
_IC_ORDER = [1, 4, 6, 7, 8, 5, 3, 2]  # Qian Zhen Kan Gen Kun Xun Li Dui
_IC_TABLE = [
    [1, 25, 6, 33, 12, 44, 13, 10],
    [34, 51, 40, 62, 16, 32, 55, 54],
    [5, 3, 29, 39, 8, 48, 63, 60],
    [26, 27, 4, 52, 23, 18, 22, 41],
    [11, 24, 7, 15, 2, 46, 36, 19],
    [9, 42, 59, 53, 20, 57, 37, 61],
    [14, 21, 64, 56, 35, 50, 30, 38],
    [43, 17, 47, 31, 45, 28, 49, 58],
]
IC_KING_WEN = {(u, l): _IC_TABLE[i][j]
               for i, u in enumerate(_IC_ORDER) for j, l in enumerate(_IC_ORDER)}
IC_NAMES = [None, "The Creative", "The Receptive", "Difficulty at the Beginning",
            "Youthful Folly", "Waiting", "Conflict", "The Army", "Holding Together",
            "Small Taming", "Treading", "Peace", "Standstill", "Fellowship",
            "Great Possession", "Modesty", "Enthusiasm", "Following",
            "Work on the Decayed", "Approach", "Contemplation", "Biting Through",
            "Grace", "Splitting Apart", "Return", "Innocence", "Great Taming",
            "Nourishment", "Great Exceeding", "The Abysmal", "The Clinging",
            "Influence", "Duration", "Retreat", "Great Power", "Progress",
            "Darkening of the Light", "The Family", "Opposition", "Obstruction",
            "Deliverance", "Decrease", "Increase", "Breakthrough",
            "Coming to Meet", "Gathering Together", "Pushing Upward",
            "Oppression", "The Well", "Revolution", "The Cauldron",
            "The Arousing", "Keeping Still", "Development", "The Marrying Maiden",
            "Abundance", "The Wanderer", "The Gentle", "The Joyous",
            "Dispersion", "Limitation", "Inner Truth", "Small Exceeding",
            "After Completion", "Before Completion"]


def ic_hexagram(lines) -> dict:
    lower = IC_LINES_TO_TRIGRAM[tuple(lines[0:3])]
    upper = IC_LINES_TO_TRIGRAM[tuple(lines[3:6])]
    n = IC_KING_WEN[(upper, lower)]
    return {"number": n, "name": IC_NAMES[n],
            "upper_trigram": IC_TRIGRAMS[upper][0],
            "lower_trigram": IC_TRIGRAMS[lower][0],
            "lines_bottom_to_top": list(lines)}


class IChingRequest(BirthDataCoords):
    day_boundary: str = Field(default="local", pattern="^(local|beijing)$")


@app.post("/iching_birth", dependencies=[Depends(verify_api_key)])
@with_engine("iching")
def iching_birth(data: IChingRequest):
    """Birth hexagram by the Plum Blossom (Mei Hua Yi Shu) time method
    from the Chinese lunisolar date and hour: primary, changed and
    nuclear hexagrams plus moving line and body/use trigrams. Method
    is school-dependent; the lunar date reuses the Zi Wei engine."""
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
        month_num, is_leap, lunar_day, lunar_year = compute_lunar_date(
            jd, local.year, tz, data.day_boundary)
        y_num = (lunar_year - 4) % 12 + 1          # Zi = 1 ... Hai = 12
        h_num = ((local.hour + 1) // 2) % 12 + 1   # Zi hour = 1
        base = y_num + month_num + lunar_day
        upper = base % 8 or 8
        lower = (base + h_num) % 8 or 8
        moving = (base + h_num) % 6 or 6

        lines = list(IC_TRIGRAMS[lower][1]) + list(IC_TRIGRAMS[upper][1])
        changed = lines.copy()
        changed[moving - 1] = 1 - changed[moving - 1]
        nuclear = lines[1:4] + lines[2:5]

        body, use = (upper, lower) if moving <= 3 else (lower, upper)
        return {
            "verification_note": (
                "Mei Hua time method: upper = (year branch + lunar month "
                "+ lunar day) mod 8, lower adds the hour branch, moving "
                "line = same total mod 6 (Early Heaven trigram numbers). "
                "Other birth-hexagram methods exist and give different "
                "results. The King Wen table passed the pair rule test "
                "(each odd/even pair is an inversion or a complement)."
            ),
            "confidence": {"level": "exact arithmetic; method school-dependent"},
            "inputs": {
                "lunar_year_branch": f"{BAZI_BRANCHES[y_num - 1]} ({y_num})",
                "lunar_month": month_num, "is_leap_month": is_leap,
                "lunar_day": lunar_day,
                "hour_branch": f"{BAZI_BRANCHES[h_num - 1]} ({h_num})",
                "day_boundary_used": data.day_boundary,
            },
            "primary_hexagram": ic_hexagram(lines),
            "moving_line": moving,
            "changed_hexagram": ic_hexagram(changed),
            "nuclear_hexagram": ic_hexagram(nuclear),
            "body_trigram": IC_TRIGRAMS[body][0],
            "use_trigram": IC_TRIGRAMS[use][0],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"I Ching calculation error: {e}")


# =====================================================
# Predictive astrology: transits, secondary progressions, solar return
# =====================================================

PRED_ASPECTS = [("conjunction", 0.0, 3.0), ("sextile", 60.0, 2.0),
                ("square", 90.0, 3.0), ("trine", 120.0, 3.0),
                ("opposition", 180.0, 3.0)]
PRED_BODIES = VEDIC_BODIES + [("true_node", swe.TRUE_NODE)]
PRED_FAST_PROGRESSED = ("sun", "moon", "mercury", "venus", "mars")


def pred_sign(lon: float) -> dict:
    return {"longitude": round(lon % 360.0, 3),
            "sign": RASHIS[int((lon % 360.0) // 30)],
            "degree_in_sign": round(lon % 30.0, 3)}


def pred_cusps(jd: float, lat: float, lng: float) -> tuple:
    cusps, ascmc = swe.houses_ex(jd, lat, lng, b"P")
    cusps = list(cusps)
    if len(cusps) == 13:            # some builds return a dummy index 0
        cusps = cusps[1:]
    return cusps, ascmc[0] % 360.0, ascmc[1] % 360.0


def pred_house(lon: float, cusps: list) -> int:
    for i in range(12):
        start, end = cusps[i], cusps[(i + 1) % 12]
        if (lon - start) % 360.0 < (end - start) % 360.0:
            return i + 1
    return 12


def pred_separation(a: float, b: float) -> float:
    return abs(((a - b + 180.0) % 360.0) - 180.0)


def pred_aspect(moving_lon: float, fixed_lon: float, orb_scale: float = 1.0):
    sep = pred_separation(moving_lon, fixed_lon)
    for name, angle, orb in PRED_ASPECTS:
        if abs(sep - angle) <= orb * orb_scale:
            return name, angle, sep - angle
    return None, None, None


def pred_positions(jd: float) -> dict:
    out = {}
    for key, body in PRED_BODIES:
        lon, speed = tropical_lon_and_speed(jd, body)
        out[key] = {"lon": lon, "speed": speed}
    return out


def pred_aspects(moving: dict, natal_points: dict, orb_scale: float,
                 only: tuple = None) -> list:
    found = []
    for mk, mv in moving.items():
        if only and mk not in only:
            continue
        for nk, nlon in natal_points.items():
            name, angle, dev = pred_aspect(mv["lon"], nlon, orb_scale)
            if not name:
                continue
            later = mv["lon"] + mv["speed"] * 0.01   # ~15 min ahead
            dev_later = pred_separation(later, nlon) - angle
            applying = abs(dev_later) < abs(dev)
            found.append({"moving": mk, "aspect": name, "natal": nk,
                          "orb": round(abs(dev), 2),
                          "applying": applying if mv["speed"] else None})
    return sorted(found, key=lambda a: a["orb"])


class PredictiveRequest(BirthDataCoords):
    target_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    target_time: str = Field(default="12:00", pattern=r"^\d{2}:\d{2}$")
    target_tz: Optional[str] = None
    return_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    return_lng: Optional[float] = Field(default=None, ge=-180, le=180)


@app.post("/predictive", dependencies=[Depends(verify_api_key)])
@with_engine("predictive")
def predictive(data: PredictiveRequest):
    """Forecast layer for a target date: transits to the natal chart
    with orbs, houses and applying/separating, secondary progressions
    with solar-arc angles, and the solar return of the target year.
    Defaults: today at 12:00 in the birth timezone."""
    try:
        tz = ZoneInfo(data.tz_str)
        ttz = ZoneInfo(data.target_tz) if data.target_tz else tz
    except Exception:
        raise HTTPException(status_code=422, detail=(
            "Unknown timezone. Use IANA format, e.g. 'Europe/Moscow' "
            "- not 'UTC+3'."
        ))
    try:
        local = datetime(data.year, data.month, data.day,
                         data.hour, data.minute, tzinfo=tz)
        ut = local.astimezone(ZoneInfo("UTC"))
        jd_birth = swe.julday(ut.year, ut.month, ut.day,
                              ut.hour + ut.minute / 60 + ut.second / 3600)

        if data.target_date:
            ty, tmo, tdy = (int(x) for x in data.target_date.split("-"))
        else:
            today = datetime.now(ttz)
            ty, tmo, tdy = today.year, today.month, today.day
        th, tmi = (int(x) for x in data.target_time.split(":"))
        target_local = datetime(ty, tmo, tdy, th, tmi, tzinfo=ttz)
        tut = target_local.astimezone(ZoneInfo("UTC"))
        jd_t = swe.julday(tut.year, tut.month, tut.day,
                          tut.hour + tut.minute / 60 + tut.second / 3600)
        if jd_t < jd_birth:
            raise HTTPException(status_code=422,
                                detail="target date is before birth")

        # --- natal reference ---
        natal = pred_positions(jd_birth)
        cusps, asc, mc = pred_cusps(jd_birth, data.lat, data.lng)
        natal_points = {k: v["lon"] for k, v in natal.items()}
        natal_points["ascendant"] = asc
        natal_points["midheaven"] = mc

        # --- transits ---
        transit = pred_positions(jd_t)
        transits_out = {}
        for k, v in transit.items():
            entry = pred_sign(v["lon"])
            entry["natal_house"] = pred_house(v["lon"], cusps)
            entry["retrograde"] = v["speed"] < 0 if k != "true_node" else None
            transits_out[k] = entry
        transit_aspects = pred_aspects(transit, natal_points, 1.0)

        # --- secondary progressions (a day for a year) ---
        age_years = (jd_t - jd_birth) / 365.2422
        prog = pred_positions(jd_birth + age_years)
        arc = (prog["sun"]["lon"] - natal["sun"]["lon"]) % 360.0
        prog_out = {k: dict(pred_sign(v["lon"]),
                            natal_house=pred_house(v["lon"], cusps))
                    for k, v in prog.items() if k in PRED_FAST_PROGRESSED}
        prog_angles = {"solar_arc_deg": round(arc, 3),
                       "midheaven": pred_sign(mc + arc),
                       "ascendant": pred_sign(asc + arc)}
        prog_moving = {k: v for k, v in prog.items() if k in PRED_FAST_PROGRESSED}
        prog_moving["midheaven"] = {"lon": (mc + arc) % 360.0, "speed": 0.0}
        prog_moving["ascendant"] = {"lon": (asc + arc) % 360.0, "speed": 0.0}
        prog_aspects = pred_aspects(prog_moving, natal_points, 1.0 / 3.0)

        # --- solar return for the target year ---
        sun_natal = natal["sun"]["lon"]
        guess = swe.julday(ty, ut.month, min(ut.day, 28),
                           ut.hour + ut.minute / 60)
        jd_sr = find_solar_term_jd(guess, sun_natal)
        if jd_sr > jd_t + 1:   # birthday later this year -> previous return
            jd_sr = find_solar_term_jd(jd_sr - 365.2422, sun_natal)
        sr_lat = data.return_lat if data.return_lat is not None else data.lat
        sr_lng = data.return_lng if data.return_lng is not None else data.lng
        sr_cusps, sr_asc, sr_mc = pred_cusps(jd_sr, sr_lat, sr_lng)
        sr_pos = pred_positions(jd_sr)
        y, mo, d, h = swe.revjul(jd_sr)
        sr_utc = datetime(y, mo, d, tzinfo=ZoneInfo("UTC")) + timedelta(hours=float(h))
        sun_err = abs(((sr_pos["sun"]["lon"] - sun_natal + 180.0) % 360.0) - 180.0)

        return {
            "verification_note": (
                "Positions come from the same Swiss Ephemeris engine as "
                "the verified natal modules. Orbs: transits 3 deg "
                "(sextile 2), progressions 1 deg (sextile 0.67). Natal "
                "houses: Placidus. Progressed angles use the solar-arc "
                "method. The solar return is the moment the Sun returns "
                "to its exact natal longitude (error reported). The "
                "most recent return before the target date is used."
            ),
            "confidence": {"level": "verified engine; new forecast module"},
            "target_moment": {"local": target_local.isoformat(),
                              "utc": tut.isoformat()},
            "transits": {"positions": transits_out,
                         "aspects_to_natal": transit_aspects},
            "progressions": {"age_years": round(age_years, 3),
                             "positions": prog_out,
                             "angles": prog_angles,
                             "aspects_to_natal": prog_aspects},
            "solar_return": {
                "moment_utc": sr_utc.isoformat(),
                "sun_error_arcsec": round(sun_err * 3600, 2),
                "location": {"lat": sr_lat, "lng": sr_lng},
                "ascendant": pred_sign(sr_asc),
                "midheaven": pred_sign(sr_mc),
                "planets": {k: dict(pred_sign(v["lon"]),
                                    house=pred_house(v["lon"], sr_cusps))
                            for k, v in sr_pos.items()},
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422,
                            detail=f"Predictive calculation error: {e}")


# =====================================================
# Qi Men Dun Jia - hour chart, Chai Bu (split-and-patch) method
# =====================================================

QM_TERMS = ["Chun Fen", "Qing Ming", "Gu Yu", "Li Xia", "Xiao Man",
            "Mang Zhong", "Xia Zhi", "Xiao Shu", "Da Shu", "Li Qiu",
            "Chu Shu", "Bai Lu", "Qiu Fen", "Han Lu", "Shuang Jiang",
            "Li Dong", "Xiao Xue", "Da Xue", "Dong Zhi", "Xiao Han",
            "Da Han", "Li Chun", "Yu Shui", "Jing Zhe"]   # every 15 deg from 0
QM_JU = {  # term -> (dun, (upper, middle, lower) yuan ju numbers)
    "Dong Zhi": ("yang", (1, 7, 4)), "Xiao Han": ("yang", (2, 8, 5)),
    "Da Han": ("yang", (3, 9, 6)), "Li Chun": ("yang", (8, 5, 2)),
    "Yu Shui": ("yang", (9, 6, 3)), "Jing Zhe": ("yang", (1, 7, 4)),
    "Chun Fen": ("yang", (3, 9, 6)), "Qing Ming": ("yang", (4, 1, 7)),
    "Gu Yu": ("yang", (5, 2, 8)), "Li Xia": ("yang", (4, 1, 7)),
    "Xiao Man": ("yang", (5, 2, 8)), "Mang Zhong": ("yang", (6, 3, 9)),
    "Xia Zhi": ("yin", (9, 3, 6)), "Xiao Shu": ("yin", (8, 2, 5)),
    "Da Shu": ("yin", (7, 1, 4)), "Li Qiu": ("yin", (2, 5, 8)),
    "Chu Shu": ("yin", (1, 4, 7)), "Bai Lu": ("yin", (9, 3, 6)),
    "Qiu Fen": ("yin", (7, 1, 4)), "Han Lu": ("yin", (6, 9, 3)),
    "Shuang Jiang": ("yin", (5, 8, 2)), "Li Dong": ("yin", (6, 9, 3)),
    "Xiao Xue": ("yin", (5, 8, 2)), "Da Xue": ("yin", (4, 7, 1)),
}
QM_EARTH_STEMS = ["Wu", "Ji", "Geng", "Xin", "Ren", "Gui", "Ding", "Bing", "Yi"]
QM_RING = [1, 8, 3, 4, 9, 2, 7, 6]         # clockwise from North
QM_DIRECTION = {1: "N", 8: "NE", 3: "E", 4: "SE", 9: "S", 2: "SW",
                7: "W", 6: "NW", 5: "Center"}
QM_STARS = {1: "Tian Peng", 8: "Tian Ren", 3: "Tian Chong", 4: "Tian Fu",
            9: "Tian Ying", 2: "Tian Rui", 7: "Tian Zhu", 6: "Tian Xin",
            5: "Tian Qin"}
QM_DOORS = {1: "Xiu (Rest)", 8: "Sheng (Life)", 3: "Shang (Harm)",
            4: "Du (Delusion)", 9: "Jing (Scenery)", 2: "Si (Death)",
            7: "Jing (Fear)", 6: "Kai (Open)"}
QM_DEITIES = ["Zhi Fu", "Teng She", "Tai Yin", "Liu He", "Bai Hu",
              "Xuan Wu", "Jiu Di", "Jiu Tian"]
QM_XUN_YI = {0: "Wu", 10: "Ji", 20: "Geng", 30: "Xin", 40: "Ren", 50: "Gui"}
QM_BRANCH_PALACE = {0: 1, 1: 8, 2: 8, 3: 3, 4: 4, 5: 4, 6: 9, 7: 2, 8: 2,
                    9: 7, 10: 6, 11: 6}
QM_HORSE = {8: 2, 0: 2, 4: 2, 2: 8, 6: 8, 10: 8, 5: 11, 9: 11, 1: 11,
            11: 5, 3: 5, 7: 5}            # San He frame -> horse branch


def qm_sexagenary(stem_idx: int, branch_idx: int) -> int:
    return next(i for i in range(60) if i % 10 == stem_idx and i % 12 == branch_idx)


def qm_step(palace: int, n: int, yang: bool) -> int:
    return ((palace - 1 + n) % 9) + 1 if yang else ((palace - 1 - n) % 9) + 1


class QimenRequest(BirthDataCoords):
    day_boundary: str = Field(default="midnight", pattern="^(midnight|zi_23)$")


@app.post("/qimen", dependencies=[Depends(verify_api_key)])
@with_engine("qimen")
def qimen(data: QimenRequest):
    """Qi Men Dun Jia hour chart (Chai Bu method) for the given moment:
    Yin/Yang Dun and Ju from the solar term and Fu Tou, earth and
    heaven plates, nine stars, eight doors, eight deities, Zhi Fu /
    Zhi Shi, void and horse. Method verified; numeric chart not yet
    compared with an external calculator."""
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

        lon = sun_longitude(jd)
        term = QM_TERMS[int(lon // 15) % 24]
        dun, ju_triple = QM_JU[term]
        yang = (dun == "yang")

        # day and hour pillars (same conventions as /bazi_chart);
        # zi_23: the late Zi hour (23:00-24:00) already belongs to the next day
        day_date = local.date()
        if data.day_boundary == "zi_23" and local.hour == 23:
            day_date = day_date + timedelta(days=1)
        offset = civil_jdn(day_date.year, day_date.month, day_date.day) - BAZI_EPOCH_JDN
        d_stem = (BAZI_EPOCH_STEM + offset) % 10
        d_branch = (BAZI_EPOCH_BRANCH + offset) % 12
        d60 = qm_sexagenary(d_stem, d_branch)
        h_branch = ((local.hour + 1) // 2) % 12
        h_stem = (BAZI_FIVE_RAT[d_stem] + h_branch) % 10
        h60 = qm_sexagenary(h_stem, h_branch)

        # Fu Tou: the latest Jia or Ji day -> upper/middle/lower yuan
        fu_tou = (d60 - (d_stem % 5)) % 60
        fb = fu_tou % 12
        yuan = 0 if fb in (0, 3, 6, 9) else (1 if fb in (2, 5, 8, 11) else 2)
        ju = ju_triple[yuan]

        earth = {qm_step(ju, k, yang): s for k, s in enumerate(QM_EARTH_STEMS)}
        stem_palace = {s: p for p, s in earth.items()}

        xun = h60 - (h60 % 10)
        xun_yi = QM_XUN_YI[xun]
        zf_palace = stem_palace[xun_yi]
        zf_ring = 2 if zf_palace == 5 else zf_palace
        hour_stem_name = BAZI_STEMS[h_stem]
        lookup = xun_yi if hour_stem_name == "Jia" else hour_stem_name
        target = stem_palace[lookup]
        target = 2 if target == 5 else target

        shift = (QM_RING.index(target) - QM_RING.index(zf_ring)) % 8
        heaven = {}
        for p in QM_RING:
            newp = QM_RING[(QM_RING.index(p) + shift) % 8]
            stars, stems = [QM_STARS[p]], [earth[p]]
            if p == 2:                     # Tian Qin lodges with Tian Rui
                stars.append(QM_STARS[5])
                stems.append(earth[5])
            heaven[newp] = {"stars": stars, "heaven_stems": stems}

        n_hours = h60 - xun
        door_p = qm_step(zf_palace, n_hours, yang)
        door_p = 2 if door_p == 5 else door_p
        dshift = (QM_RING.index(door_p) - QM_RING.index(zf_ring)) % 8
        doors = {QM_RING[(QM_RING.index(p) + dshift) % 8]: QM_DOORS[p]
                 for p in QM_RING}

        start = QM_RING.index(target)
        deities = {}
        for i, name in enumerate(QM_DEITIES):
            idx = (start + i) % 8 if yang else (start - i) % 8
            deities[QM_RING[idx]] = name

        xb = xun % 12
        void_branches = [(xb + 10) % 12, (xb + 11) % 12]
        horse_branch = QM_HORSE[h_branch]

        palaces = []
        for p in (4, 9, 2, 3, 5, 7, 8, 1, 6):
            entry = {"palace": p, "direction": QM_DIRECTION[p],
                     "earth_stem": earth[p]}
            if p != 5:
                entry.update({
                    "heaven_stems": heaven[p]["heaven_stems"],
                    "stars": heaven[p]["stars"],
                    "door": doors[p],
                    "deity": deities[p],
                    "void": any(QM_BRANCH_PALACE[b] == p for b in void_branches),
                    "horse": QM_BRANCH_PALACE[horse_branch] == p,
                })
            palaces.append(entry)

        return {
            "verification_note": (
                "Algorithm matches a published open Chai Bu algorithm "
                "step by step and the upper-yuan Ju numbers match the "
                "classical verse; no numeric reference chart compared "
                "yet - verify Dun, Ju, Zhi Fu, Zhi Shi and two palaces "
                "against a Qi Men calculator. The Zhi Run method can "
                "give a different Ju near term boundaries. Clock time, "
                "no true-solar-time correction. day_boundary: midnight "
                "(default) or zi_23 (23:00 starts the next day). Tian "
                "Qin (centre) lodges with Tian Rui (Kun palace)."
            ),
            "confidence": {"level": "method verified; chart not yet compared"},
            "day_boundary_used": data.day_boundary,
            "solar_term": term,
            "dun": dun,
            "yuan": ["upper", "middle", "lower"][yuan],
            "ju": ju,
            "day_pillar": f"{BAZI_STEMS[d_stem]} {BAZI_BRANCHES[d_branch]}",
            "hour_pillar": f"{hour_stem_name} {BAZI_BRANCHES[h_branch]}",
            "xun_leader": f"Jia {BAZI_BRANCHES[xun % 12]} (hidden under {xun_yi})",
            "zhi_fu_star": QM_STARS[zf_palace],
            "zhi_shi_door": QM_DOORS[zf_ring],
            "void_branches": [BAZI_BRANCHES[b] for b in void_branches],
            "horse_branch": BAZI_BRANCHES[horse_branch],
            "palaces_lo_shu_layout": palaces,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Qi Men calculation error: {e}")


# =====================================================
# Psychometrics: Big Five (Mini-IPIP) and Schwartz values (PVQ-21)
# =====================================================
# These are questionnaire layers: independent of birth data, they give
# self-report evidence that the symbolic systems cannot.

BIG5_ITEMS = [  # (trait, keyed, English public-domain IPIP text, Russian)
    ("E", 1, "Am the life of the party.", "Я душа компании."),
    ("A", 1, "Sympathize with others' feelings.", "Я сочувствую переживаниям других."),
    ("C", 1, "Get chores done right away.", "Я сразу выполняю повседневные дела."),
    ("N", 1, "Have frequent mood swings.", "У меня часто меняется настроение."),
    ("I", 1, "Have a vivid imagination.", "У меня яркое воображение."),
    ("E", -1, "Don't talk a lot.", "Я мало говорю."),
    ("A", -1, "Am not interested in other people's problems.", "Меня не интересуют чужие проблемы."),
    ("C", -1, "Often forget to put things back in their proper place.", "Я часто забываю класть вещи на место."),
    ("N", -1, "Am relaxed most of the time.", "Большую часть времени я спокоен(на)."),
    ("I", -1, "Am not interested in abstract ideas.", "Меня не интересуют абстрактные идеи."),
    ("E", 1, "Talk to a lot of different people at parties.", "На встречах я общаюсь со множеством разных людей."),
    ("A", 1, "Feel others' emotions.", "Я чувствую эмоции других людей."),
    ("C", 1, "Like order.", "Я люблю порядок."),
    ("N", 1, "Get upset easily.", "Я легко расстраиваюсь."),
    ("I", -1, "Have difficulty understanding abstract ideas.", "Мне трудно понимать абстрактные идеи."),
    ("E", -1, "Keep in the background.", "Я держусь в тени."),
    ("A", -1, "Am not really interested in others.", "Мне не очень интересны другие люди."),
    ("C", -1, "Make a mess of things.", "Я устраиваю беспорядок."),
    ("N", -1, "Seldom feel blue.", "Мне редко бывает грустно."),
    ("I", -1, "Do not have a good imagination.", "У меня бедное воображение."),
]
BIG5_TRAITS = {"E": "Extraversion", "A": "Agreeableness", "C": "Conscientiousness",
               "N": "Neuroticism (emotional reactivity)", "I": "Intellect / Imagination"}
BIG5_SCALE = {1: "Very inaccurate", 2: "Moderately inaccurate",
              3: "Neither accurate nor inaccurate", 4: "Moderately accurate",
              5: "Very accurate"}

PVQ_VALUES = {  # PVQ-21 (ESS) item numbers per basic value
    "Self-Direction": (1, 11), "Power": (2, 17), "Universalism": (3, 8, 19),
    "Achievement": (4, 13), "Security": (5, 14), "Stimulation": (6, 15),
    "Conformity": (7, 16), "Tradition": (9, 20), "Hedonism": (10, 21),
    "Benevolence": (12, 18),
}
PVQ_HIGHER = {
    "Openness to Change": ("Self-Direction", "Stimulation", "Hedonism"),
    "Self-Enhancement": ("Achievement", "Power"),
    "Conservation": ("Security", "Conformity", "Tradition"),
    "Self-Transcendence": ("Universalism", "Benevolence"),
}


@app.get("/big_five/items", dependencies=[Depends(verify_api_key)])
@with_engine("big_five")
def big_five_items():
    """Return the 20 Mini-IPIP Big Five items (public-domain IPIP) with
    a Russian working translation and the 1-5 answer scale. Ask them
    in this order, then send the answers to POST /big_five."""
    return {
        "instructions": ("Ask the user to rate how accurately each "
                         "statement describes them, 1-5. Keep the order."),
        "scale": BIG5_SCALE,
        "items": [{"id": i + 1, "text_en": en, "text_ru": ru}
                  for i, (_, _, en, ru) in enumerate(BIG5_ITEMS)],
        "note": ("Items: Mini-IPIP (Donnellan et al., 2006), IPIP public "
                 "domain. The Russian wording is a working translation, "
                 "not a validated adaptation."),
    }


class BigFiveRequest(BaseModel):
    answers: list[int] = Field(min_length=20, max_length=20)


@app.post("/big_five", dependencies=[Depends(verify_api_key)])
@with_engine("big_five")
def big_five(data: BigFiveRequest):
    """Score the 20 Mini-IPIP answers (1-5, in item order) into the
    Big Five traits: mean 1-5, a 0-100 scale position and a level band.
    Includes a simple response-quality check. No population norms."""
    if any(a < 1 or a > 5 for a in data.answers):
        raise HTTPException(status_code=422, detail="answers must be 1-5")
    sums = {t: [] for t in BIG5_TRAITS}
    for (trait, keyed, _, _), a in zip(BIG5_ITEMS, data.answers):
        sums[trait].append(a if keyed == 1 else 6 - a)
    traits = {}
    for t, vals in sums.items():
        mean = sum(vals) / len(vals)
        pos = (mean - 1) / 4 * 100
        band = "low" if mean < 2.5 else ("high" if mean > 3.5 else "middle")
        traits[BIG5_TRAITS[t]] = {"mean_1_5": round(mean, 2),
                                  "scale_position_0_100": round(pos),
                                  "band": band}
    same = max(data.answers.count(v) for v in range(1, 6))
    return {
        "verification_note": (
            "Exact scoring of the Mini-IPIP key. Scale positions are NOT "
            "percentiles: no population norms are applied. Four items "
            "per trait give a short, coarse measure (reliability about "
            ".6-.8 in the source study)."
        ),
        "confidence": {"level": "exact scoring; short self-report scale"},
        "traits": traits,
        "response_quality": {
            "most_repeated_answer_count": same,
            "warning": ("possible straight-lining" if same >= 16 else None),
        },
    }


class SchwartzRequest(BaseModel):
    answers: list[int] = Field(min_length=21, max_length=21)


@app.post("/schwartz_values", dependencies=[Depends(verify_api_key)])
@with_engine("schwartz_values")
def schwartz_values(data: SchwartzRequest):
    """Score the 21 PVQ-21 (ESS) answers in item order, 1-6 where
    6 = very much like me: 10 basic values, centred scores and the 4
    higher-order dimensions. Use the official PVQ-21 wording."""
    if any(a < 1 or a > 6 for a in data.answers):
        raise HTTPException(status_code=422, detail="answers must be 1-6")
    overall = sum(data.answers) / 21.0
    values = {}
    for name, items in PVQ_VALUES.items():
        raw = sum(data.answers[i - 1] for i in items) / len(items)
        values[name] = {"raw_mean": round(raw, 2),
                        "centred": round(raw - overall, 2)}
    ranking = sorted(values, key=lambda v: values[v]["centred"], reverse=True)
    higher = {h: round(sum(values[v]["centred"] for v in parts) / len(parts), 2)
              for h, parts in PVQ_HIGHER.items()}
    return {
        "verification_note": (
            "Exact PVQ-21 scoring with within-person centring (the "
            "procedure Schwartz recommends: compare a value with the "
            "person's own mean, not across people). Hedonism is counted "
            "under Openness to Change. PVQ items are Schwartz's; check "
            "the licence before commercial use."
        ),
        "confidence": {"level": "exact scoring; self-report scale"},
        "overall_mean": round(overall, 2),
        "basic_values": values,
        "priority_ranking": ranking,
        "higher_order": higher,
    }


@app.get("/systems", dependencies=[Depends(verify_api_key)])
def systems():
    """Catalog of all systems in the 17-system canon: operation names,
    engine, method and conventions, verification status, plus the
    ephemeris backend in use. Call it to know what is calculated and
    how reliable each layer is."""
    return {
        "server_version": SERVER_VERSION,
        "ephemeris": ephemeris_backend(),
        "canon_17": ENGINES,
        "symbolic_layers_without_engine": SYMBOLIC_LAYERS,
        "note": ("Symbolic layers are interpretation only. A system "
                 "without an engine must never be presented as a "
                 "calculation."),
    }


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """<h1>Privacy Policy</h1>
    <p>This service calculates astrological and Human Design charts
    from birth data provided by the user. Data is processed in memory
    only and is not stored, logged, or shared with third parties.</p>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
