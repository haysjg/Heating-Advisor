"""Tests du worker d'apprentissage automatique du COP."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modules import cop_auto_learning


@pytest.fixture
def config():
    return SimpleNamespace(
        LOCATION={"city": "Test", "postal_code": "00000", "latitude": 45.0, "longitude": 5.0},
        COP_LEARNING={"auto_learning": {}},
    )


@pytest.fixture(autouse=True)
def reset_worker_state():
    cop_auto_learning._worker_status["current_sampling_task_id"] = None
    cop_auto_learning._worker_status["stats"]["total_samples_started"] = 0
    cop_auto_learning._rejection_history.clear()
    yield


class TestDelayedSampling:
    """_delayed_sampling : récupération de la température puis lancement de l'échantillonnage."""

    @patch("modules.cop_auto_learning.time.sleep")
    @patch("modules.cop_auto_learning._can_start_sampling", return_value=(True, "OK"))
    @patch("modules.cop_auto_learning.cop_sampling.start_sampling_task", return_value="task-1")
    @patch("modules.cop_auto_learning.weather.get_current_temperature")
    def test_passes_location_and_extracts_temperature(self, mock_weather, mock_start, _can, _sleep, config):
        mock_weather.return_value = {"temperature": 8.2, "source": "Open-Meteo", "error": None}

        cop_auto_learning._delayed_sampling(config, datetime.now(), 0)

        mock_weather.assert_called_once_with(config.LOCATION)
        assert mock_start.call_args.kwargs["outdoor_temp"] == 8.2
        assert cop_auto_learning._worker_status["current_sampling_task_id"] == "task-1"
        assert cop_auto_learning._rejection_history == []

    @patch("modules.cop_auto_learning.time.sleep")
    @patch("modules.cop_auto_learning._can_start_sampling", return_value=(True, "OK"))
    @patch("modules.cop_auto_learning.cop_sampling.start_sampling_task")
    @patch("modules.cop_auto_learning.weather.get_current_temperature")
    def test_rejects_when_temperature_unavailable(self, mock_weather, mock_start, _can, _sleep, config):
        mock_weather.return_value = {"temperature": None, "source": None, "error": "inaccessible"}

        cop_auto_learning._delayed_sampling(config, datetime.now(), 0)

        mock_start.assert_not_called()
        assert cop_auto_learning._rejection_history[0]["reason_code"] == "no_outdoor_temp"
