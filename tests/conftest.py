"""Fixtures partagées pour les tests unitaires."""

import pytest


@pytest.fixture
def base_config():
    """Configuration complète du Heating Advisor pour les tests."""
    return {
        "CLIM": {
            "model": "Test Clim",
            "nominal_capacity_kw": 4.0,
            "nominal_cop": 2.8,
            "min_outdoor_temp": -10,
            "comfort_min_temp": 7,
            "cop_curve": [
                (-10, 1.5),
                (-7, 1.8),
                (0, 2.2),
                (7, 2.8),
                (12, 3.2),
                (20, 3.8),
            ],
        },
        "POELE": {
            "pellet_price_per_kg": 0.4233,
            "consumption_kg_per_hour": 1.0,
            "efficiency": 0.90,
            "thermal_output_kw": 7.2,
        },
        "TEMPO_PRICES": {
            "BLUE": {"HP": 0.1369, "HC": 0.1056},
            "WHITE": {"HP": 0.1894, "HC": 0.1259},
            "RED": {"HP": 0.7561, "HC": 0.1369},
            "UNKNOWN": {"HP": 0.1894, "HC": 0.1259},
        },
        "HP_START": 6,
        "HP_END": 22,
        "NO_HEATING_AT_NIGHT": True,
        "TARGET_TEMP": 21,
        "COP_LEARNING": {"enabled": False},
    }


@pytest.fixture
def base_thermostat_cfg():
    """Configuration thermostat pour les tests."""
    return {
        "enabled": True,
        "temp_on": 20.0,
        "temp_off": 22.9,
        "min_on_minutes": 90,
        "end_of_schedule_grace_minutes": 45,
        "check_interval_minutes": 10,
        "manual_off_suspend_hours": 4,
        "presence_enabled": False,
        "use_felt_temperature": True,
        "humidity_reference": 50.0,
        "humidity_correction_factor": 0.05,
        "schedule": {
            "mon": {"start": "05:45", "end": "22:00"},
            "tue": {"start": "05:45", "end": "22:00"},
            "wed": {"start": "06:00", "end": "22:00"},
            "thu": {"start": "05:45", "end": "22:00"},
            "fri": {"start": "05:45", "end": "22:00"},
            "sat": {"start": "07:00", "end": "22:00"},
            "sun": {"start": "07:00", "end": "22:00"},
        },
    }


@pytest.fixture
def base_ha_cfg():
    """Configuration Home Assistant minimale pour les tests."""
    return {
        "enabled": True,
        "url": "http://192.168.1.2:8123",
        "token": "test-token",
        "poele_entity_id": "climate.test_poele",
        "auto_control": False,
        "shelly_temp_entity_id": "sensor.test_temp",
        "shelly_humidity_entity_id": "sensor.test_humidity",
    }
