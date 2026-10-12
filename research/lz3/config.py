"""Configuration and constants for Lorentzian v3 research."""

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

# 16 Fixed Grid Configurations
GRID_CONFIGS = [
    {
        "config_id": f"cfg_{i:02d}",
        "l_fast": l_fast,
        "l_slow": l_slow,
        "n_breakout": n_breakout,
        "atr_mult": atr_mult,
        "carry_mode": carry_mode,
    }
    for i, (l_fast, l_slow, n_breakout, atr_mult, carry_mode) in enumerate(
        (
            (lf, ls, nb, am, cm)
            for lf, ls in [(12, 48), (24, 96)]
            for nb in [18, 36]
            for am in [2.0, 2.5]
            for cm in ["On", "Off"]
        ),
        start=1,
    )
]


def rt(sym: str) -> float:
    """Round-trip friction: 2 * (fee + slip) / 10000."""
    slip = SLIP_BPS.get(sym, DEFAULT_SLIP_BPS)
    return 2.0 * (FEE_BPS + slip) / 1e4


def get_dev_cutoff_ms() -> int:
    """Return DEV_END as epoch milliseconds in UTC."""
    return int(pd.Timestamp(DEV_END, tz="UTC").timestamp() * 1000)


def load_asset_v3(sym: str, split: str = "dev") -> dict[str, pd.DataFrame]:
    """Load klines and funding data under strict SOL isolation and dev cutoff guards.

    Args:
        sym: Symbol name (e.g. 'BTCUSDT').
        split: 'dev' or 'holdout'.

    Raises:
        ValueError: If split is not 'dev' or 'holdout'.
        PermissionError: If dev split tries to load SOLUSDT / non-dev assets,
                         or if holdout is called without LZ_HOLDOUT_APPROVED='yes'.
        FileNotFoundError: If required parquet files are missing.
    """
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

