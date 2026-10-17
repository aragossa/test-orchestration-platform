import pytest
from pydantic import ValidationError

from app.domain.config import RunConfig
from app.domain.states import FailureSimulation


def valid_payload() -> dict[str, object]:
    return {
        "environment": "test",
        "deployment_version": "latest",
        "worker_instances": 2,
        "data_generators": 1,
        "timeout_seconds": 60,
        "selected_test_ids": ["connectivity_check", "data_ingestion"],
        "failure_simulation": "none",
    }


def test_valid_config_is_accepted_and_frozen() -> None:
    config = RunConfig.model_validate(valid_payload())
    assert config.failure_simulation is FailureSimulation.NONE
    assert config.selected_test_ids == ("connectivity_check", "data_ingestion")
    with pytest.raises(ValidationError):
        config.worker_instances = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_instances", 0),
        ("worker_instances", 6),
        ("data_generators", 0),
        ("data_generators", 6),
        ("timeout_seconds", 0),
        ("timeout_seconds", -5),
        ("selected_test_ids", []),
        ("selected_test_ids", ["a", "a"]),
        ("deployment_version", "   "),
        ("failure_simulation", "explode"),
        ("environment", ""),
    ],
)
def test_invalid_values_are_rejected(field: str, value: object) -> None:
    payload = valid_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        RunConfig.model_validate(payload)


def test_unknown_fields_are_rejected() -> None:
    payload = valid_payload()
    payload["surprise"] = True
    with pytest.raises(ValidationError):
        RunConfig.model_validate(payload)


def test_version_is_stripped() -> None:
    payload = valid_payload()
    payload["deployment_version"] = "  stable  "
    assert RunConfig.model_validate(payload).deployment_version == "stable"
