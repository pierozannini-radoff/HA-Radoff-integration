"""
Which Cognito pool and app client a login goes to.

Covers: the pool read from the environment, the fallback to the shipped
defaults, and a half-set environment.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pycognito.aws_srp import AWSSRP

from custom_components.radoff.api import API
from custom_components.radoff.const import (
    DEFAULT_CLIENT_ID,
    DEFAULT_POOL_ID,
    DEFAULT_POOL_REGION,
)

from .conftest import auth_result, make_id_token

# Synthetic: the real dev pool lives only in the developer's `.env`.
ENV_POOL_ID = "eu-central-1_EnvPool01"
ENV_CLIENT_ID = "env-client-id"


def _login_target(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Log in once with a mocked handshake and return where it was sent."""
    target: dict[str, Any] = {}

    def _fake_authenticate_user(self: AWSSRP) -> dict[str, Any]:
        target["pool_id"] = self.pool_id
        target["client_id"] = self.client_id
        target["region"] = self.client.meta.region_name
        return auth_result(make_id_token(["test-domain-1"]))

    monkeypatch.setattr(AWSSRP, "authenticate_user", _fake_authenticate_user)
    API(username="user@example.com", password="hunter2").connect()
    return target


def test_a_pool_set_in_the_environment_is_where_the_login_goes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both variables set: pool, client and the region read off the pool id."""
    monkeypatch.setenv("RADOFF_DEV_POOL_ID", ENV_POOL_ID)
    monkeypatch.setenv("RADOFF_DEV_CLIENT_ID", ENV_CLIENT_ID)

    assert _login_target(monkeypatch) == {
        "pool_id": ENV_POOL_ID,
        "client_id": ENV_CLIENT_ID,
        "region": "eu-central-1",
    }


def test_without_the_environment_the_login_uses_the_shipped_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither variable set: the defaults distributed with the integration."""
    monkeypatch.delenv("RADOFF_DEV_POOL_ID", raising=False)
    monkeypatch.delenv("RADOFF_DEV_CLIENT_ID", raising=False)

    assert _login_target(monkeypatch) == {
        "pool_id": DEFAULT_POOL_ID,
        "client_id": DEFAULT_CLIENT_ID,
        "region": DEFAULT_POOL_REGION,
    }


def test_half_an_environment_is_ignored_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One variable set: shipped pool, one warning naming the missing one, no value."""
    monkeypatch.setenv("RADOFF_DEV_POOL_ID", ENV_POOL_ID)
    monkeypatch.delenv("RADOFF_DEV_CLIENT_ID", raising=False)

    with caplog.at_level(logging.WARNING, logger="custom_components.radoff"):
        target = _login_target(monkeypatch)

    assert target["pool_id"] == DEFAULT_POOL_ID
    assert target["client_id"] == DEFAULT_CLIENT_ID
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "RADOFF_DEV_CLIENT_ID" in warnings[0].getMessage()
    assert ENV_POOL_ID not in caplog.text
