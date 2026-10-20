PLACES = (
    {"id": "builtin:moscow", "name": "Москва", "latitude": 55.7558, "longitude": 37.6173},
    {"id": "builtin:london", "name": "Лондон", "latitude": 51.5074, "longitude": -0.1278},
    {"id": "builtin:paris", "name": "Париж", "latitude": 48.8566, "longitude": 2.3522},
    {"id": "builtin:tokyo", "name": "Токио", "latitude": 35.6762, "longitude": 139.6503},
)


def places_search(query: str) -> dict:
    if not 2 <= len(query.strip()) <= 100:
        raise ValueError("Нужно 2–100 знаков")
    return {
        "places": [p for p in PLACES if query.casefold().strip() in p["name"].casefold()],
        "source": "geoosint-builtin-v1",
        "coverage": "4 справочных города",
        "limitations": [
            "Приблизительные центры, не границы и не место съёмки",
            "Nominatim и внешнее геокодирование не подключены",
        ],
    }
