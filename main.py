import os

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from kerykeion import AstrologicalSubject
from pydantic import BaseModel, Field

app = FastAPI(
    title="Astro Server",
    description="Natal chart calculation service for Custom GPT",
    version="3.0",
)

# ============================================
# Secrets are read from environment variables.
# Set them in Render: service -> Environment:
#   GEONAMES_USERNAME = your geonames.org login
#   API_KEY           = any long random string
# ============================================
GEONAMES_USERNAME = os.environ.get("ne_fact", "")
API_KEY = os.environ.get("124568390865378656789123567890", "")


# ---------- API key check ----------

def verify_api_key(x_api_key: str = Header(default="")):
    """Requests must include header 'X-API-Key' with the correct key.
    If API_KEY env var is not set, the check is skipped (open mode)."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------- Input models (with validation) ----------

class BirthData(BaseModel):
    name: str
    year: int = Field(ge=1000, le=2100)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    city: str          # Latin spelling, e.g. "Moscow"
    nation: str = ""   # country code, e.g. "RU" (recommended)


class BirthDataCoords(BaseModel):
    name: str
    year: int = Field(ge=1000, le=2100)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    lat: float = Field(ge=-90, le=90)     # e.g. 55.7558
    lng: float = Field(ge=-180, le=180)   # e.g. 37.6173
    tz_str: str                           # e.g. "Europe/Moscow"
    city: str = "Custom location"


# ---------- Shared response builder ----------

def build_response(s: AstrologicalSubject) -> dict:
    return {
        "location_check": {
            "resolved_city": s.city,
            "country": s.nation,
            "lat": s.lat,
            "lng": s.lng,
            "timezone": s.tz_str,
        },
        "planets": {
            "sun": s.sun,
            "moon": s.moon,
            "mercury": s.mercury,
            "venus": s.venus,
            "mars": s.mars,
            "jupiter": s.jupiter,
            "saturn": s.saturn,
            "uranus": s.uranus,
            "neptune": s.neptune,
            "pluto": s.pluto,
        },
        "ascendant": s.first_house,
        "houses": s.houses_list,
    }


# ---------- Main endpoint: by city name ----------

@app.post("/natal_chart", dependencies=[Depends(verify_api_key)])
def natal_chart(data: BirthData):
    """Calculate a natal chart by city name (resolved via GeoNames).
    Always verify location_check in the response: make sure the
    resolved city, country and timezone are correct. If they are
    wrong, call /natal_chart_coords instead."""
    try:
        s = AstrologicalSubject(
            data.name, data.year, data.month, data.day,
            data.hour, data.minute, data.city, data.nation,
            geonames_username=GEONAMES_USERNAME,
        )
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Could not resolve city '{data.city}': {e}. "
                "Try the Latin spelling with a country code (nation), "
                "or call /natal_chart_coords with coordinates "
                "and a timezone."
            ),
        )
    return build_response(s)


# ---------- Fallback endpoint: by coordinates ----------

@app.post("/natal_chart_coords", dependencies=[Depends(verify_api_key)])
def natal_chart_coords(data: BirthDataCoords):
    """Reliable calculation without geocoding: latitude, longitude
    and timezone are provided directly. Use this if /natal_chart
    resolved the wrong city or returned an error."""
    try:
        s = AstrologicalSubject(
            data.name, data.year, data.month, data.day,
            data.hour, data.minute,
            lat=data.lat, lng=data.lng, tz_str=data.tz_str,
            city=data.city, online=False,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Calculation error: {e}")
    return build_response(s)


# ---------- Privacy policy (public, no key needed) ----------

@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """<h1>Privacy Policy</h1>
    <p>This service calculates astrological charts from birth data
    provided by the user. Data is processed in memory only and is
    not stored, logged, or shared with third parties.</p>"""


# ---------- Local run (not required on Render) ----------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
