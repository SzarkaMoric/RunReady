from app.main import (
    aqi_score,
    calculate_runability_score,
    limiting_reason,
    rain_score,
    rain_status,
    rain_status_code,
    recommendation_code,
    score_components,
    score_explanation,
    score_status_code,
)


def test_score_is_clamped_between_0_and_100():
    high = calculate_runability_score(aqi=10, temp_c=18, humidity=50, wind_kmh=5, precip_mm=0)
    low = calculate_runability_score(aqi=500, temp_c=-20, humidity=100, wind_kmh=80, precip_mm=20)

    assert 0 <= high <= 100
    assert 0 <= low <= 100


def test_score_prefers_ideal_running_conditions():
    ideal = calculate_runability_score(aqi=10, temp_c=18, humidity=50, wind_kmh=5, precip_mm=0)
    poor = calculate_runability_score(aqi=200, temp_c=35, humidity=95, wind_kmh=45, precip_mm=5)

    assert ideal > poor


def test_rain_reduces_score():
    dry = rain_score(0)
    wet = rain_score(3)

    assert dry == 1.0
    assert wet < dry


def test_aqi_uses_health_risk_bands():
    assert aqi_score(50) == 1.0
    assert aqi_score(75) == 0.75
    assert aqi_score(125) == 0.45
    assert aqi_score(175) == 0.20
    assert aqi_score(225) == 0.05


def test_explanation_names_weakest_factor():
    components = score_components(aqi=180, temp_c=18, humidity=50, wind_kmh=5, precip_mm=0)
    weakest_factor, status, explanation = score_explanation(50, components)

    assert weakest_factor == "air quality"
    assert status == "Marginal"
    assert "air quality" in explanation


def test_aqi_zero_scores_as_clean_air():
    assert aqi_score(0) == 1.0


def test_rain_status_uses_precipitation_amount():
    assert rain_status(0) == "Not raining"
    assert rain_status(0.1) == "Raining"
    assert rain_status_code(0) == 0
    assert rain_status_code(0.1) == 1


def test_status_codes_match_score_bands():
    assert score_status_code(85) == 3
    assert score_status_code(65) == 2
    assert score_status_code(45) == 1
    assert score_status_code(25) == 0
    assert recommendation_code(85) == 3
    assert recommendation_code(65) == 2
    assert recommendation_code(45) == 1
    assert recommendation_code(25) == 0


def test_limiting_reason_uses_human_weather_labels():
    assert limiting_reason(temp_c=4, humidity=50, wind_kmh=5, precip_mm=0, weakest_factor="temperature") == (1, "Low temp")
    assert limiting_reason(temp_c=31, humidity=50, wind_kmh=5, precip_mm=0, weakest_factor="temperature") == (2, "High temp")
    assert limiting_reason(temp_c=18, humidity=50, wind_kmh=5, precip_mm=0, weakest_factor="air quality") == (3, "Bad air quality")
    assert limiting_reason(temp_c=18, humidity=95, wind_kmh=5, precip_mm=0, weakest_factor="humidity") == (4, "High humidity")
    assert limiting_reason(temp_c=18, humidity=50, wind_kmh=5, precip_mm=2, weakest_factor="rain") == (5, "Raining")
    assert limiting_reason(temp_c=18, humidity=50, wind_kmh=40, precip_mm=0, weakest_factor="wind") == (6, "Windy")
