# Simple Runability Homework Project

This is a **small homework project** using Python + InfluxDB + Grafana.

It does one simple flow:
1. Read current AQI from WAQI
2. Read current weather from Open-Meteo
3. Read today hourly and 7-day weather forecasts from Open-Meteo
4. Compute one current Runability Score (0-100) from AQI, temperature, humidity, and wind
5. Save values to InfluxDB
6. Show them in Grafana

## Files You Need to Understand

- `app/main.py` - single script for API calls, score calculation, and Influx write
- `docker-compose.yml` - starts InfluxDB, Grafana, and app
- `.env.example` - environment variables
- `grafana/provisioning/datasources/influxdb.yml` - Grafana datasource
- `grafana/provisioning/dashboards/runability-overview.json` - simple dashboard

## Quick Setup (Under 10 Steps)

1. Copy env file:
   ```bash
   cp .env.example .env
   ```
2. Put your WAQI token into `.env` (`WAQI_TOKEN=...`).
3. (Optional) change location with:
   - `WAQI_CITY_SLUG`
   - `LATITUDE`, `LONGITUDE`
4. Start everything:
   ```bash
   docker compose up --build -d
   ```
5. Open Grafana: `http://localhost:3000`
6. Login with `admin / admin`
7. Open dashboard: **Runability Homework Dashboard**

## What Gets Stored in InfluxDB

- `air_quality_raw` with `aqi`
- `weather_raw` with `temperature_c`, `humidity`, `wind_kmh`, `precip_mm`, `rain_status`, `rain_status_code`
- `weather_forecast_hourly` with hourly `temperature_c`, `humidity`, `wind_kmh`, `precip_mm`
- `weather_forecast_daily` with daily `temp_min_c`, `temp_max_c`, `precip_sum_mm`, `wind_max_kmh`
- `runability_score` with `score`, `score_status`, `score_status_code`, `recommendation_text`, `recommendation_code`, `limiting_reason_text`, `limiting_reason_code`, `score_explanation`, `weakest_factor`, and component score fields

## Expected Output Example

When the app runs, it prints lines like:
- `AQI: 61.0`
- `Temp: 18.2 C | Wind: 9.1 km/h | Rain: 0.0 mm`
- `Runability Score: 81.4 -> Great for run/walk`

## Demo Checklist for Homework

- `docker compose up -d` starts all services
- app prints AQI + weather + score
- InfluxDB has points in all 3 measurements
- Grafana shows:
  - Runability Score
  - Current AQI
  - Weather Snapshot (Temp C)
