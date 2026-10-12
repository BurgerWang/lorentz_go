"""Configuration and constants for Lorentzian v5 research."""

import os
from pathlib import Path
import pandas as pd

DEV_ASSETS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT"]
HOLDOUT_ASSET = "SOLUSDT"
ALL_ASSETS = DEV_ASSETS + [HOLDOUT_ASSET]

BAR_MS = 4 * 3600 * 1000
KLINE_START = "2021-09-01"
DEV_SCORE_START = "2023-01-01"
DEV_END = "2025-01-01"
HOLDOUT_END = "2026-10-01"

FEE_BPS = 5.0
SLIP_BPS = {"BTCUSDT": 2.0, "ETHUSDT": 2.0}
DEFAULT_SLIP_BPS = 4.0

DATA_DIR = "research/data"
OUT_DIR = "research/out"

# 8 Fixed Grid Configurations
GRID_CONFIGS = [
    {"config_id": "cfg_01", "w_mom": 18, "t_reb": 6, "lambda_carry": 0.5},
    {"config_id": "cfg_02", "w_mom": 18, "t_reb": 6, "lambda_carry": 1.0},
    {"config_id": "cfg_03", "w_mom": 18, "t_reb": 12, "lambda_carry": 0.5},
    {"config_id": "cfg_04", "w_mom": 18, "t_reb": 12, "lambda_carry": 1.0},
    {"config_id": "cfg_05", "w_mom": 42, "t_reb": 6, "lambda_carry": 0.5},
    {"config_id": "cfg_06", "w_mom": 42, "t_reb": 6, "lambda_carry": 1.0},
    {"config_id": "cfg_07", "w_mom": 42, "t_reb": 12, "lambda_carry": 0.5},
    {"config_id": "cfg_08", "w_mom": 42, "t_reb": 12, "lambda_carry": 1.0},
]


def get_slippage_bps(sym: str) -> float:
    """Return asset-specific slippage in bps (2.0 bps for BTC/ETH, 4.0 bps others)."""
    return SLIP_BPS.get(sym, DEFAULT_SLIP_BPS)


def rt(sym: str) -> float:
    """Round-trip friction: 2 * (fee + slip) / 10000."""
    return 2.0 * (FEE_BPS + get_slippage_bps(sym)) / 1e4


def one_way_friction(sym: str, cost_mult: float = 1.0) -> float:
    """One-way friction factor: (FEE_BPS + slip_bps) * 1e-4 * cost_mult."""
    return (FEE_BPS + get_slippage_bps(sym)) * 1e-4 * cost_mult


def get_dev_cutoff_ms() -> int:
    """Return DEV_END as epoch milliseconds in UTC."""
    return int(pd.Timestamp(DEV_END, tz="UTC").timestamp() * 1000)


def load_asset_v5(sym: str, split: str = "dev") -> dict[str, pd.DataFrame]:
    """Load klines and funding data under strict SOL isolation and dev cutoff guards."""
    if split not in ("dev", "holdout"):
        raise ValueError(f"Invalid split '{split}', expected 'dev' or 'holdout'")

    if split == "dev":
        if sym not in DEV_ASSETS:
            raise PermissionError(f"Symbol '{sym}' is strictly forbidden in dev split (SOL isolation)")
    elif split == "holdout":
        if os.environ.get("LZ_HOLDOUT_APPROVED") != "yes":
            raise PermissionError("Holdout access requires environment variable LZ_HOLDOUT_APPROVED='yes'")

    data_path = Path(DATA_DIR)
    kline_file = data_path / "klines_4h" / f"{sym}.parquet"
    funding_file = data_path / "funding" / f"{sym}.parquet"

    if not kline_file.exists():
        raise FileNotFoundError(f"Missing klines file: {kline_file}")
    if not funding_file.exists():
        raise FileNotFoundError(f"Missing funding file: {funding_file}")

    klines = pd.read_parquet(kline_file)
    funding = pd.read_parquet(funding_file)

    if split == "dev":
        cutoff_ms = get_dev_cutoff_ms()
        klines = klines[klines["open_time"] < cutoff_ms].copy()
        funding = funding[funding["calc_time"] < cutoff_ms].copy()

    klines["sym"] = sym
    funding["sym"] = sym

    return {
        "klines": klines,
        "funding": funding,
    }


def load_universe_v5(split: str = "dev") -> dict[str, dict[str, pd.DataFrame]]:
    """Load all assets for the given split under strict security guards."""
    symbols = DEV_ASSETS if split == "dev" else ALL_ASSETS
    return {s: load_asset_v5(s, split=split) for s in symbols}
