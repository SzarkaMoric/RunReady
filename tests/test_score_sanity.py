from app.main import calculate_runability_score


def test_score_is_clamped_between_0_and_100():
    high = calculate_runability_score(aqi=10, temp_c=18, wind_kmh=5, precip_mm=0)
    low = calculate_runability_score(aqi=500, temp_c=-20, wind_kmh=80, precip_mm=20)

    assert 0 <= high <= 100
    assert 0 <= low <= 100
