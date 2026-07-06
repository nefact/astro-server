from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from kerykeion import AstrologicalSubject
from pydantic import BaseModel

app = FastAPI(
    title="Astro Server",
    description="Расчёт натальной карты для Custom GPT",
    version="2.0",
)

# ============================================
# ВАЖНО: впиши сюда свой логин с geonames.org
# (и не забудь включить free web services
# на странице geonames.org/manageaccount)
# ============================================
GEONAMES_USERNAME = "твой_логин"


# ---------- Модели входных данных ----------

class BirthData(BaseModel):
    name: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    city: str          # латиницей, например "Moscow"
    nation: str = ""   # код страны, например "RU" (желательно указывать)


class BirthDataCoords(BaseModel):
    name: str
    year: int
    month: int
    day: int
    hour: int
    minute: int
    lat: float         # широта, например 55.7558
    lng: float         # долгота, например 37.6173
    tz_str: str        # часовой пояс, например "Europe/Moscow"
    city: str = "Custom location"


# ---------- Общая сборка ответа ----------

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


# ---------- Основной метод: по названию города ----------

@app.post("/natal_chart")
def natal_chart(data: BirthData):
    """Расчёт натальной карты по названию города (через GeoNames).
    Всегда проверяй location_check в ответе: тот ли город и часовой
    пояс найден. Если нет — используй /natal_chart_coords."""
    try:
        s = AstrologicalSubject(
            data.name, data.year, data.month, data.day,
            data.hour, data.minute, data.city, data.nation,
            geonames_username="ne_fact",
        )
    except Exception as e:
        return {
            "error": (
                f"Не удалось определить город '{data.city}': {e}. "
                "Попробуй название латиницей с кодом страны (nation), "
                "либо вызови /natal_chart_coords с координатами "
                "и часовым поясом."
            )
        }
    return build_response(s)


# ---------- Запасной метод: по координатам ----------

@app.post("/natal_chart_coords")
def natal_chart_coords(data: BirthDataCoords):
    """Надёжный расчёт без геокодинга: широта, долгота и часовой пояс
    задаются напрямую. Используй, если /natal_chart нашёл не тот город
    или вернул ошибку."""
    try:
        s = AstrologicalSubject(
            data.name, data.year, data.month, data.day,
            data.hour, data.minute,
            lat=data.lat, lng=data.lng, tz_str=data.tz_str,
            city=data.city, online=False,
        )
    except Exception as e:
        return {"error": f"Ошибка расчёта: {e}"}
    return build_response(s)


# ---------- Политика конфиденциальности ----------

@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """<h1>Privacy Policy</h1>
    <p>This service calculates astrological charts from birth data
    provided by the user. Data is processed in memory only and is
    not stored, logged, or shared with third parties.</p>"""


# ---------- Локальный запуск (для Render не обязателен) ----------

if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
