import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS


collection_lock = threading.Lock()

LOCATION_PRESETS = {
    "bme_campus": {
        "location_id": "bme_campus",
        "location_name": "BME Campus",
        "latitude": 47.4791,
        "longitude": 19.0579,
    },
    "budapest_downtown": {
        "location_id": "budapest_downtown",
        "location_name": "Budapest Downtown",
        "latitude": 47.4979,
        "longitude": 19.0402,
    },
    "margaret_island": {
        "location_id": "margaret_island",
        "location_name": "Margaret Island",
        "latitude": 47.5281,
        "longitude": 19.0500,
    },
    "kopaszi_gat": {
        "location_id": "kopaszi_gat",
        "location_name": "Kopaszi-gat",
        "latitude": 47.4685,
        "longitude": 19.0624,
    },
    "obuda": {
        "location_id": "obuda",
        "location_name": "Óbuda (3rd District)",
        "latitude": 47.5410,
        "longitude": 19.0450,
    },
}


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


def rain_status(precip_mm: float, rain_soon: bool = False) -> str:
    if precip_mm > 0:
        return "Raining now"
    if rain_soon:
        return "Rain soon"
    return "Not raining"


def rain_status_code(precip_mm: float, rain_soon: bool = False) -> int:
    if precip_mm > 0:
        return 2
    if rain_soon:
        return 1
    return 0


def rain_expected_soon(forecast: list[dict], current_time: datetime, hours: int = 2) -> bool:
    window_end = current_time + timedelta(hours=hours)
    return any(
        current_time < forecast_row["time"] <= window_end and forecast_row["precip_mm"] > 0
        for forecast_row in forecast
    )


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


def score_explanation(score: float, components: dict) -> tuple[str, str, str]:
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
        "forecast_days": 2,
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


def load_config() -> dict:
    load_dotenv()
    return {
        "waqi_token": os.getenv("WAQI_TOKEN", "demo"),
        "city_slug": os.getenv("WAQI_CITY_SLUG", "budapest"),
        "location_id": os.getenv("LOCATION_ID", "uni_main"),
        "location_name": os.getenv("LOCATION_NAME", "University"),
        "latitude": float(os.getenv("LATITUDE", "47.4979")),
        "longitude": float(os.getenv("LONGITUDE", "19.0402")),
        "influx_url": os.getenv("INFLUX_URL", "http://influxdb:8086"),
        "influx_token": os.getenv("INFLUX_TOKEN", "runability-super-secret-token"),
        "influx_org": os.getenv("INFLUX_ORG", "runability"),
        "influx_bucket": os.getenv("INFLUX_BUCKET", "runability"),
    }


def resolve_location_config(config: dict, requested_location_id: str | None = None) -> dict:
    location = dict(config)
    if requested_location_id:
        preset = LOCATION_PRESETS.get(requested_location_id)
        if not preset:
            raise ValueError(f"Unknown location_id: {requested_location_id}")
        location.update(preset)
        location["city_slug"] = f"geo:{preset['latitude']};{preset['longitude']}"
    return location


def collect_once(config: dict, writer, requested_location_id: str | None = None) -> dict:
    with collection_lock:
        location_config = resolve_location_config(config, requested_location_id)
        aqi = fetch_waqi_current(location_config["waqi_token"], location_config["city_slug"])
        weather = fetch_openmeteo_current(location_config["latitude"], location_config["longitude"])
        forecast = fetch_openmeteo_hourly_forecast(location_config["latitude"], location_config["longitude"])
        daily_forecast = fetch_openmeteo_daily_forecast(location_config["latitude"], location_config["longitude"])
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
        rain_soon = rain_expected_soon(forecast, now)

        common_tags = {
            "location_id": location_config["location_id"],
            "location_name": location_config["location_name"],
        }
        point_aqi = Point("air_quality_raw").time(now).tag("source", "waqi")
        point_weather = Point("weather_raw").time(now).tag("source", "open_meteo")
        point_score = Point("runability_score").time(now).tag("model_version", "v1")
        forecast_points = []
        runability_forecast_points = []
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
            .field("rain_status", rain_status(weather["precip_mm"], rain_soon))
            .field("rain_status_code", rain_status_code(weather["precip_mm"], rain_soon))
        )
        point_score = (
            point_score.field("score", score)
            .field("latitude", location_config["latitude"])
            .field("longitude", location_config["longitude"])
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
            forecast_score = calculate_runability_score(
                aqi=aqi,
                temp_c=forecast_row["temperature_c"],
                humidity=forecast_row["humidity"],
                wind_kmh=forecast_row["wind_kmh"],
                precip_mm=forecast_row["precip_mm"],
            )
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
            point_runability_forecast = (
                Point("runability_forecast_hourly")
                .time(forecast_row["time"])
                .tag("source", "open_meteo")
                .tag("model_version", "v1")
                .field("score", forecast_score)
                .field("estimated_aqi", aqi)
                .field("temperature_c", forecast_row["temperature_c"])
                .field("humidity", forecast_row["humidity"])
                .field("wind_kmh", forecast_row["wind_kmh"])
                .field("precip_mm", forecast_row["precip_mm"])
            )
            for k, v in common_tags.items():
                point_forecast = point_forecast.tag(k, v)
                point_runability_forecast = point_runability_forecast.tag(k, v)
            forecast_points.append(point_forecast)
            runability_forecast_points.append(point_runability_forecast)

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
            bucket=config["influx_bucket"],
            org=config["influx_org"],
            record=[
                point_aqi,
                point_weather,
                point_score,
                *forecast_points,
                *runability_forecast_points,
                *daily_forecast_points,
            ],
        )

        result = {
            "time": now.isoformat(),
            "location_id": location_config["location_id"],
            "location_name": location_config["location_name"],
            "latitude": location_config["latitude"],
            "longitude": location_config["longitude"],
            "aqi": aqi,
            "temperature_c": weather["temperature_c"],
            "humidity": weather["humidity"],
            "wind_kmh": weather["wind_kmh"],
            "precip_mm": weather["precip_mm"],
            "score": score,
            "recommendation": advice,
            "limiting_reason": reason_text,
            "hourly_forecast_points": len(forecast_points),
            "runability_forecast_points": len(runability_forecast_points),
            "daily_forecast_points": len(daily_forecast_points),
        }

        print(f"[{now}] Location: {location_config['location_name']} ({location_config['location_id']})")
        print(f"[{now}] AQI: {aqi}")
        print(
            f"[{now}] Temp: {weather['temperature_c']} C | Humidity: {weather['humidity']}% "
            f"| Wind: {weather['wind_kmh']} km/h | Rain: {weather['precip_mm']} mm"
        )
        print(f"[{now}] Runability Score: {score} -> {advice}")
        print(f"[{now}] {explanation}")
        print(f"[{now}] Limiting reason: {reason_text}")
        print(f"[{now}] Stored {len(forecast_points)} hourly forecast points")
        print(f"[{now}] Stored {len(runability_forecast_points)} hourly runability forecast points")
        print(f"[{now}] Stored {len(daily_forecast_points)} daily forecast points")

        return result


def start_collect_server(config: dict, writer) -> HTTPServer:
    class CollectHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self) -> None:
            self._send_json(200, {"ok": True})

        def do_GET(self) -> None:
            self._handle_collect()

        def do_POST(self) -> None:
            self._handle_collect()

        def _handle_collect(self) -> None:
            parsed_url = urlparse(self.path)
            if parsed_url.path not in ("/collect", "/collect/"):
                self._send_json(404, {"ok": False, "error": "Not found"})
                return
            params = parse_qs(parsed_url.query)
            requested_location_id = params.get("location_id", [None])[0]
            try:
                self._send_json(200, {"ok": True, "data": collect_once(config, writer, requested_location_id)})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args) -> None:
            print(f"[collect-api] {self.address_string()} - {format % args}")

    server = HTTPServer(("0.0.0.0", 8000), CollectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Collect API listening on http://0.0.0.0:8000/collect")
    return server


def collect_all_locations(config: dict, writer) -> None:
    for location_id in LOCATION_PRESETS:
        try:
            collect_once(config, writer, location_id)
        except Exception as exc:
            print(f"Error collecting {location_id}: {exc}")


def main() -> None:
    config = load_config()
    client = InfluxDBClient(url=config["influx_url"], token=config["influx_token"], org=config["influx_org"])
    writer = client.write_api(write_options=SYNCHRONOUS)
    server = start_collect_server(config, writer)

    try:
        while True:
            try:
                collect_all_locations(config, writer)
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
        server.shutdown()
        client.close()


if __name__ == "__main__":
    main()
