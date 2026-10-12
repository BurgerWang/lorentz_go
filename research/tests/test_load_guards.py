"""Tests for split guards and SOL isolation in load.py."""

import os
import pytest
import pandas as pd

from lz.config import DEV_ASSETS, HOLDOUT_ASSET, DEV_END
from lz.load import load_asset, get_dev_cutoff_ms


def test_sol_in_dev_raises_permission_error():
    with pytest.raises(PermissionError, match="forbidden in dev"):
        load_asset(HOLDOUT_ASSET, split="dev")


def test_holdout_without_env_raises_permission_error(monkeypatch):
    monkeypatch.delenv("LZ_HOLDOUT_APPROVED", raising=False)
    with pytest.raises(PermissionError, match="LZ_HOLDOUT_APPROVED"):
        load_asset("BTCUSDT", split="holdout")
    with pytest.raises(PermissionError, match="LZ_HOLDOUT_APPROVED"):
        load_asset(HOLDOUT_ASSET, split="holdout")


def test_invalid_split_raises_value_error():
    with pytest.raises(ValueError, match="Invalid split"):
        load_asset("BTCUSDT", split="test")


def test_dev_data_truncated_before_2025():
    cutoff_ms = get_dev_cutoff_ms()
    expected_cutoff = int(pd.Timestamp(DEV_END, tz="UTC").timestamp() * 1000)
    assert cutoff_ms == expected_cutoff

    # For any available dev asset, test truncation if files exist
    for sym in DEV_ASSETS:
        try:
            data = load_asset(sym, split="dev")
        except FileNotFoundError:
            continue
        klines = data["klines"]
        funding = data["funding"]
        metrics = data["metrics"]
        if len(klines) > 0:
            assert klines["open_time"].max() < cutoff_ms
        if len(funding) > 0:
            assert funding["calc_time"].max() < cutoff_ms
        if len(metrics) > 0:
            assert metrics["ts"].max() < cutoff_ms
