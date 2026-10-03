# verifies: FR-001-01, FR-001-05, FR-001-08, FR-001-14, FR-001-16
"""Версіонована конфігурація збору: config/ingest.yaml -> IngestConfig."""

from pathlib import Path

import pytest
import yaml

from unmask.ingest.config import ConfigError, IngestConfig, load_config

SHIPPED = Path(__file__).resolve().parent.parent / "config" / "ingest.yaml"
CHANGELOG = Path(__file__).resolve().parent.parent / "config" / "CHANGELOG.md"

BASE = {
    "version": 1,
    "first_buyers_n": 300,
    "funding_depth": 2,
    "counterparty_threshold": 200,
    "max_signatures_per_wallet": 300,
    "collect_spl_inbound": True,
    "time_budget_seconds": 40,
    "commitment": "finalized",
    "rpc": {
        "page_size": 1000,
        "request_timeout_seconds": 10,
        "max_retries": 2,
        "retry_backoff_seconds": 0.5,
        "max_concurrency": 8,
    },
}


def _write(tmp_path, data) -> Path:
    path = tmp_path / "ingest.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _variant(**changes):
    data = {**BASE, "rpc": dict(BASE["rpc"])}
    for key, value in changes.items():
        if key.startswith("rpc."):
            data["rpc"][key[4:]] = value
        else:
            data[key] = value
    return data


def test_shipped_config_loads_with_version_1():
    cfg = load_config(SHIPPED)

    assert isinstance(cfg, IngestConfig)
    assert cfg.version == 1
    assert cfg.first_buyers_n == 300
    assert cfg.funding_depth == 2
    assert cfg.counterparty_threshold == 200
    assert cfg.max_signatures_per_wallet == 300
    assert cfg.collect_spl_inbound is True
    assert cfg.time_budget_seconds == 40
    assert cfg.commitment == "finalized"
    assert cfg.rpc.page_size == 1000
    assert cfg.rpc.request_timeout_seconds == 10
    assert cfg.rpc.max_retries == 2
    assert cfg.rpc.retry_backoff_seconds == 0.5
    assert cfg.rpc.max_concurrency == 8
    # принцип III: версія має запис у changelog
    assert "## 1 " in CHANGELOG.read_text(encoding="utf-8")


def test_each_field_round_trips_from_yaml(tmp_path):
    data = {
        "version": 7,
        "first_buyers_n": 250,
        "funding_depth": 3,
        "counterparty_threshold": 150,
        "max_signatures_per_wallet": 123,
        "collect_spl_inbound": False,
        "time_budget_seconds": 12.5,
        "commitment": "confirmed",
        "rpc": {
            "page_size": 500,
            "request_timeout_seconds": 3.5,
            "max_retries": 0,
            "retry_backoff_seconds": 0,
            "max_concurrency": 4,
        },
    }

    cfg = load_config(_write(tmp_path, data))

    assert cfg.version == 7
    assert cfg.first_buyers_n == 250
    assert cfg.funding_depth == 3
    assert cfg.counterparty_threshold == 150
    assert cfg.max_signatures_per_wallet == 123
    assert cfg.collect_spl_inbound is False
    assert cfg.time_budget_seconds == 12.5
    assert cfg.commitment == "confirmed"
    assert cfg.rpc.page_size == 500
    assert cfg.rpc.request_timeout_seconds == 3.5
    assert cfg.rpc.max_retries == 0
    assert cfg.rpc.retry_backoff_seconds == 0
    assert cfg.rpc.max_concurrency == 4


def _without(key):
    data = _variant()
    if key.startswith("rpc."):
        del data["rpc"][key[4:]]
    else:
        del data[key]
    return data


def _with_unknown(where):
    data = _variant()
    if where == "rpc":
        data["rpc"]["bogus_field"] = 1
    else:
        data["bogus_field"] = 1
    return data


@pytest.mark.parametrize(
    ("data", "field"),
    [
        pytest.param(_variant(first_buyers_n=0), "first_buyers_n", id="N=0"),
        pytest.param(_variant(first_buyers_n=501), "first_buyers_n", id="N=501"),
        pytest.param(_variant(funding_depth=0), "funding_depth", id="depth=0"),
        pytest.param(_variant(funding_depth=4), "funding_depth", id="depth=4"),
        pytest.param(_variant(version=0), "version", id="version=0"),
        pytest.param(_variant(counterparty_threshold=0), "counterparty_threshold", id="threshold=0"),
        pytest.param(_variant(max_signatures_per_wallet=0), "max_signatures_per_wallet", id="max_sigs=0"),
        pytest.param(_variant(time_budget_seconds=0), "time_budget_seconds", id="budget=0"),
        pytest.param(_variant(commitment="processed"), "commitment", id="commitment=processed"),
        pytest.param(_variant(**{"rpc.page_size": 0}), "page_size", id="page_size=0"),
        pytest.param(_variant(**{"rpc.page_size": 1001}), "page_size", id="page_size=1001"),
        pytest.param(_variant(collect_spl_inbound="yes"), "collect_spl_inbound", id="spl_not_bool"),
        pytest.param(_without("version"), "version", id="missing-version"),
        pytest.param(_without("rpc.page_size"), "page_size", id="missing-rpc-field"),
        pytest.param(_with_unknown("top"), "bogus_field", id="unknown-field"),
        pytest.param(_with_unknown("rpc"), "bogus_field", id="unknown-rpc-field"),
    ],
)
def test_out_of_range_raises_config_error(tmp_path, data, field):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, data))

    assert field in str(excinfo.value)
