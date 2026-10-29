"""Failure diagnostics must not disclose the live API credentials."""

from _pytest._io.saferepr import saferepr

from .live.test_live_api import LiveConfiguration


def test_live_fixture_failure_representation_hides_credentials():
    config = LiveConfiguration(
        base="https://127.0.0.1:5002", apiKey="private-api-key", secret="private-api-secret"
    )

    displayed = saferepr(config)

    assert "private-api-key" not in displayed
    assert "private-api-secret" not in displayed
    assert "https://127.0.0.1:5002" in displayed
    assert config["apiKey"] == "private-api-key"
    assert config["secret"] == "private-api-secret"
