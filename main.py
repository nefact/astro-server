from fastapi import FastAPI
from kerykeion import AstrologicalSubject
from pydantic import BaseModel

app = FastAPI()
from fastapi.responses import HTMLResponse

@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """<h1>Privacy Policy</h1>
    <p>This service calculates astrological charts from birth data
    provided by the user. Data is processed in memory only and is
    not stored, logged, or shared with third parties.</p>"""


class BirthData(BaseModel):
    name: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    city: str
    nation: str = ""

@app.post("/natal_chart")
def natal_chart(data: BirthData):
    s = AstrologicalSubject(
        data.name, data.year, data.month, data.day,
        data.hour, data.minute, data.city, data.nation
    )
    return {
        "sun": s.sun, "moon": s.moon, "mercury": s.mercury,
        "venus": s.venus, "mars": s.mars, "jupiter": s.jupiter,
        "saturn": s.saturn, "uranus": s.uranus,
        "neptune": s.neptune, "pluto": s.pluto,
        "asc": s.first_house,
    }
