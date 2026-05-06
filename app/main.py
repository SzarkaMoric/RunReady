import math
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS


def clamp_score(value: float) -> float: 
    return max(0.0, min(100.0, round(value, 2)))


def bell_score(value: float, ideal: float, tolerance: float) -> float:
    return math.exp(-((value - ideal) / tolerance) ** 2)


def inverse_score(value: float, safe_limit: float) -> float:
    return 1 / (1 + (value / safe_limit) ** 2)


def rain_score(precip_mm: float) -> float:
    if precip_mm <= 0:
        return 1.0
    return 1 / (1 + (precip_mm / 1.5) ** 2)


def rain_status(precip_mm: float) -> str:
    if precip_mm > 0:
        return "Raining"
    return "Not raining"


def rain_status_code(precip_mm: float) -> int:
    if precip_mm > 0:
        return 1
    return 0


def aqi_score(aqi: float) -> float:
    if aqi <= 50:
        return 1.0
    if aqi <= 100:
        return 0.75
    if aqi <= 150:
        return 0.45
    if aqi <= 200:
        return 0.20
    return 0.05


def score_components(aqi: float, temp_c: float, humidity: float, wind_kmh: float, precip_mm: float) -> dict:
    return {
        "temperature": bell_score(temp_c, ideal=18, tolerance=9),
        "humidity": bell_score(humidity, ideal=50, tolerance=22),
        "wind": inverse_score(wind_kmh, safe_limit=22),
        "air_quality": aqi_score(aqi),
        "rain": rain_score(precip_mm),
    }


def calculate_runability_score(aqi: float, temp_c: float, humidity: float, wind_kmh: float, precip_mm: float) -> float:
    components = score_components(aqi, temp_c, humidity, wind_kmh, precip_mm)
    score = 100
    for component_score in components.values():
        score *= component_score
    return clamp_score(score)


def score_explanation(score: float, components: dict) -> tuple[str, str]:
    weakest_factor = min(components, key=components.get).replace("_", " ")

    if score >= 80:
        status = "Good"
        explanation = f"Good conditions. Weakest factor: {weakest_factor}."
    elif score >= 60:
        status = "Usable"
        explanation = f"Usable conditions, mainly limited by {weakest_factor}."
    elif score >= 40:
        status = "Marginal"
        explanation = f"Marginal conditions. Biggest issue: {weakest_factor}."
    else:
        status = "Poor"
        explanation = f"Poor conditions. Biggest issue: {weakest_factor}."

    return weakest_factor, status, explanation


def limiting_reason(temp_c: float, humidity: float, wind_kmh: float, precip_mm: float, weakest_factor: str) -> tuple[int, str]:
    if weakest_factor == "temperature":
        if temp_c < 18:
            return 1, "Low temp"
        return 2, "High temp"
    if weakest_factor == "air quality":
        return 3, "Bad air quality"
    if weakest_factor == "humidity":
        return 4, "High humidity"
    if weakest_factor == "rain":
        return 5, "Raining"
    if weakest_factor == "wind":
        return 6, "Windy"
    return 0, "Good"


def fetch_waqi_current(token: str, city_slug: str) -> float:
    url = f"https://api.waqi.info/feed/{city_slug}/?token={token}"
    payload = requests.get(url, timeout=10).json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"WAQI error: {payload}")

    data = payload["data"]
    pollutant_keys = ("pm25", "pm10", "o3", "no2", "so2", "co")
    candidates = [data.get("aqi")]
    candidates.extend(
        data.get("iaqi", {}).get(pollutant_key, {}).get("v")
        for pollutant_key in pollutant_keys
    )

    for value in candidates:
        try:
            return float(value)
        except (TypeError, ValueError):
            continue

    raise RuntimeError(f"WAQI response did not include a numeric AQI or pollutant AQI: {data}")


def fetch_openmeteo_current(latitude: float, longitude: float) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation",
        "timezone": "UTC",
    }
    payload = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10).json()
    current = payload["current"]
    return {
        "temperature_c": float(current.get("temperature_2m", 0.0)),
        "humidity": float(current.get("relative_humidity_2m", 0.0)),
        "wind_kmh": float(current.get("wind_speed_10m", 0.0)),
        "precip_mm": float(current.get("precipitation", 0.0)),
    }


def fetch_openmeteo_hourly_forecast(latitude: float, longitude: float) -> list[dict]:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation",
        "timezone": "UTC",
        "forecast_days": 1,
    }
    payload = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10).json()
    hourly = payload["hourly"]
    rows = []

    for index, forecast_time in enumerate(hourly.get("time", [])):
        rows.append(
            {
                "time": datetime.fromisoformat(forecast_time).replace(tzinfo=timezone.utc),
                "temperature_c": float(hourly["temperature_2m"][index]),
                "humidity": float(hourly["relative_humidity_2m"][index]),
                "wind_kmh": float(hourly["wind_speed_10m"][index]),
                "precip_mm": float(hourly["precipitation"][index]),
            }
        )

    return rows


def fetch_openmeteo_daily_forecast(latitude: float, longitude: float) -> list[dict]:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_min,temperature_2m_max,precipitation_sum,wind_speed_10m_max",
        "timezone": "UTC",
        "forecast_days": 7,
    }
    payload = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10).json()
    daily = payload["daily"]
    rows = []

    for index, forecast_date in enumerate(daily.get("time", [])):
        rows.append(
            {
                "time": datetime.fromisoformat(forecast_date).replace(tzinfo=timezone.utc),
                "temp_min_c": float(daily["temperature_2m_min"][index]),
                "temp_max_c": float(daily["temperature_2m_max"][index]),
                "precip_sum_mm": float(daily["precipitation_sum"][index]),
                "wind_max_kmh": float(daily["wind_speed_10m_max"][index]),
            }
        )

    return rows


def recommendation(score: float) -> str:
    if score >= 80:
        return "Great for run/walk"
    if score >= 60:
        return "Okay with caution"
    if score >= 40:
        return "Short/light activity only"
    return "Not recommended now"


def recommendation_code(score: float) -> int:
    if score >= 80:
        return 3
    if score >= 60:
        return 2
    if score >= 40:
        return 1
    return 0


def score_status_code(score: float) -> int:
    if score >= 80:
        return 3
    if score >= 60:
        return 2
    if score >= 40:
        return 1
    return 0


def main() -> None:
    load_dotenv()
    waqi_token = os.getenv("WAQI_TOKEN", "demo")
    city_slug = os.getenv("WAQI_CITY_SLUG", "budapest")
    location_id = os.getenv("LOCATION_ID", "uni_main")
    location_name = os.getenv("LOCATION_NAME", "University")
    latitude = float(os.getenv("LATITUDE", "47.4979"))
    longitude = float(os.getenv("LONGITUDE", "19.0402"))
    influx_url = os.getenv("INFLUX_URL", "http://influxdb:8086")
    influx_token = os.getenv("INFLUX_TOKEN", "runability-super-secret-token")
    influx_org = os.getenv("INFLUX_ORG", "runability")
    influx_bucket = os.getenv("INFLUX_BUCKET", "runability")

    client = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org)
    writer = client.write_api(write_options=SYNCHRONOUS)

    try:
        while True:
            try:
                aqi = fetch_waqi_current(waqi_token, city_slug)
                weather = fetch_openmeteo_current(latitude, longitude)
                forecast = fetch_openmeteo_hourly_forecast(latitude, longitude)
                daily_forecast = fetch_openmeteo_daily_forecast(latitude, longitude)
                components = score_components(
                    aqi=aqi,
                    temp_c=weather["temperature_c"],
                    humidity=weather["humidity"],
                    wind_kmh=weather["wind_kmh"],
                    precip_mm=weather["precip_mm"],
                )
                score = calculate_runability_score(
                    aqi=aqi,
                    temp_c=weather["temperature_c"],
                    humidity=weather["humidity"],
                    wind_kmh=weather["wind_kmh"],
                    precip_mm=weather["precip_mm"],
                )
                advice = recommendation(score)
                weakest_factor, status, explanation = score_explanation(score, components)
                reason_code, reason_text = limiting_reason(
                    temp_c=weather["temperature_c"],
                    humidity=weather["humidity"],
                    wind_kmh=weather["wind_kmh"],
                    precip_mm=weather["precip_mm"],
                    weakest_factor=weakest_factor,
                )
                now = datetime.now(timezone.utc)

                common_tags = {"location_id": location_id, "location_name": location_name}
                point_aqi = Point("air_quality_raw").time(now).tag("source", "waqi")
                point_weather = Point("weather_raw").time(now).tag("source", "open_meteo")
                point_score = Point("runability_score").time(now).tag("model_version", "v1")
                forecast_points = []
                daily_forecast_points = []

                for k, v in common_tags.items():    
                    point_aqi = point_aqi.tag(k, v)
                    point_weather = point_weather.tag(k, v)
                    point_score = point_score.tag(k, v)

                point_aqi = point_aqi.field("aqi", aqi)
                point_weather = (
                    point_weather.field("temperature_c", weather["temperature_c"])
                    .field("humidity", weather["humidity"])
                    .field("wind_kmh", weather["wind_kmh"])
                    .field("precip_mm", weather["precip_mm"])
                    .field("rain_status", rain_status(weather["precip_mm"]))
                    .field("rain_status_code", rain_status_code(weather["precip_mm"]))
                )
                point_score = (
                    point_score.field("score", score)
                    .field("recommendation_text", advice)
                    .field("recommendation_code", recommendation_code(score))
                    .field("score_status", status)
                    .field("score_status_code", score_status_code(score))
                    .field("limiting_reason_code", reason_code)
                    .field("limiting_reason_text", reason_text)
                    .field("weakest_factor", weakest_factor)
                    .field("score_explanation", explanation)
                )
                for component_name, component_score in components.items():
                    point_score = point_score.field(f"{component_name}_score", round(component_score * 100, 2))

                for forecast_row in forecast:
                    point_forecast = (
                        Point("weather_forecast_hourly")
                        .time(forecast_row["time"])
                        .tag("source", "open_meteo")
                        .field("temperature_c", forecast_row["temperature_c"])
                        .field("humidity", forecast_row["humidity"])
                        .field("wind_kmh", forecast_row["wind_kmh"])
                        .field("precip_mm", forecast_row["precip_mm"])
                        .field("rain_status_code", rain_status_code(forecast_row["precip_mm"]))
                    )
                    for k, v in common_tags.items():
                        point_forecast = point_forecast.tag(k, v)
                    forecast_points.append(point_forecast)

                for forecast_row in daily_forecast:
                    point_daily_forecast = (
                        Point("weather_forecast_daily")
                        .time(forecast_row["time"])
                        .tag("source", "open_meteo")
                        .field("temp_min_c", forecast_row["temp_min_c"])
                        .field("temp_max_c", forecast_row["temp_max_c"])
                        .field("precip_sum_mm", forecast_row["precip_sum_mm"])
                        .field("wind_max_kmh", forecast_row["wind_max_kmh"])
                        .field("rain_status_code", rain_status_code(forecast_row["precip_sum_mm"]))
                    )
                    for k, v in common_tags.items():
                        point_daily_forecast = point_daily_forecast.tag(k, v)
                    daily_forecast_points.append(point_daily_forecast)

                writer.write(
                    bucket=influx_bucket,
                    org=influx_org,
                    record=[point_aqi, point_weather, point_score, *forecast_points, *daily_forecast_points],
                )

                print(f"[{now}] AQI: {aqi}")
                print(
                    f"[{now}] Temp: {weather['temperature_c']} C | Humidity: {weather['humidity']}% "
                    f"| Wind: {weather['wind_kmh']} km/h | Rain: {weather['precip_mm']} mm"
                )
                print(f"[{now}] Runability Score: {score} -> {advice}")
                print(f"[{now}] {explanation}")
                print(f"[{now}] Limiting reason: {reason_text}")
                print(f"[{now}] Stored {len(forecast_points)} hourly forecast points")
                print(f"[{now}] Stored {len(daily_forecast_points)} daily forecast points")
                print("Sleeping for 15 minutes...")

            except Exception as e:
                print(f"Error during data collection: {e}")
                print("Retrying in 5 minutes...")
                time.sleep(300)  # Retry after 5 minutes on error
                continue

            time.sleep(900)  # 15 minutes

    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        client.close()


if __name__ == "__main__":
    main()
