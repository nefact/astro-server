from fastapi import FastAPI
from kerykeion import AstrologicalSubject
from pydantic import BaseModel

app = FastAPI()

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
