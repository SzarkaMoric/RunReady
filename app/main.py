import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS


def clamp_score(value: float) -> float: 
    return max(0.0, min(100.0, round(value, 2)))


def calculate_runability_score(aqi: float, temp_c: float, wind_kmh: float, precip_mm: float) -> float:
    score = 100.0
    score -= min(100.0, aqi / 3.0) * 0.45
    if temp_c < 8:
        score -= min(30.0, (8 - temp_c) * 2.0)
    elif temp_c > 24:
        score -= min(30.0, (temp_c - 24) * 2.0)
    if wind_kmh > 20:
        score -= min(20.0, (wind_kmh - 20) * 0.8)
    score -= min(20.0, precip_mm * 8.0)

    if aqi >= 200:
        score = min(score, 20.0)
    return clamp_score(score)


def fetch_waqi_current(token: str, city_slug: str) -> float:
    url = f"https://api.waqi.info/feed/{city_slug}/?token={token}"
    payload = requests.get(url, timeout=10).json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"WAQI error: {payload}")
    return float(payload["data"].get("aqi", 0.0))


def fetch_openmeteo_current(latitude: float, longitude: float) -> dict:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,wind_speed_10m,precipitation",
        "timezone": "UTC",
    }
    payload = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10).json()
    current = payload["current"]
    return {
        "temperature_c": float(current.get("temperature_2m", 0.0)),
        "wind_kmh": float(current.get("wind_speed_10m", 0.0)),
        "precip_mm": float(current.get("precipitation", 0.0)),
    }


def recommendation(score: float) -> str:
    if score >= 80:
        return "Great for run/walk"
    if score >= 60:
        return "Okay with caution"
    if score >= 40:
        return "Short/light activity only"
    return "Not recommended now"


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
                score = calculate_runability_score(
                    aqi=aqi,
                    temp_c=weather["temperature_c"],
                    wind_kmh=weather["wind_kmh"],
                    precip_mm=weather["precip_mm"],
                )
                advice = recommendation(score)
                now = datetime.now(timezone.utc)

                common_tags = {"location_id": location_id, "location_name": location_name}
                point_aqi = Point("air_quality_raw").time(now).tag("source", "waqi")
                point_weather = Point("weather_raw").time(now).tag("source", "open_meteo")
                point_score = Point("runability_score").time(now).tag("model_version", "v1")

                for k, v in common_tags.items():
                    point_aqi = point_aqi.tag(k, v)
                    point_weather = point_weather.tag(k, v)
                    point_score = point_score.tag(k, v)

                point_aqi = point_aqi.field("aqi", aqi)
                point_weather = (
                    point_weather.field("temperature_c", weather["temperature_c"])
                    .field("wind_kmh", weather["wind_kmh"])
                    .field("precip_mm", weather["precip_mm"])
                )
                point_score = point_score.field("score", score).field("recommendation_text", advice)

                writer.write(bucket=influx_bucket, org=influx_org, record=[point_aqi, point_weather, point_score])

                print(f"[{now}] AQI: {aqi}")
                print(f"[{now}] Temp: {weather['temperature_c']} C | Wind: {weather['wind_kmh']} km/h | Rain: {weather['precip_mm']} mm")
                print(f"[{now}] Runability Score: {score} -> {advice}")
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
