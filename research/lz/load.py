"""Data loader with strict isolation guards."""

import os
from pathlib import Path
import pandas as pd

from lz.config import (
    DATA_DIR,
    DEV_ASSETS,
    DEV_END,
    HOLDOUT_ASSET,
)


def get_dev_cutoff_ms() -> int:
    """Return DEV_END as epoch milliseconds in UTC."""
    return int(pd.Timestamp(DEV_END, tz="UTC").timestamp() * 1000)


def load_asset(sym: str, split: str = "dev") -> dict[str, pd.DataFrame]:
    """Load klines, funding, and metrics data for a symbol under strict split guards.

    Args:
        sym: Symbol name (e.g. 'BTCUSDT').
        split: 'dev' or 'holdout'.

    Raises:
        ValueError: If split is not 'dev' or 'holdout'.
        PermissionError: If dev tries to access SOLUSDT / non-dev assets,
                         or if holdout is called without LZ_HOLDOUT_APPROVED == 'yes'.
        FileNotFoundError: If required parquet files are missing.
    """
    if split not in ("dev", "holdout"):
        raise ValueError(f"Invalid split '{split}', expected 'dev' or 'holdout'")

    if split == "dev":
        if sym not in DEV_ASSETS:
            raise PermissionError(f"Symbol '{sym}' is forbidden in dev split (SOL is holdout)")
    elif split == "holdout":
        if os.environ.get("LZ_HOLDOUT_APPROVED") != "yes":
            raise PermissionError("Holdout access requires environment variable LZ_HOLDOUT_APPROVED='yes'")

    data_path = Path(DATA_DIR)
    kline_file = data_path / "klines_4h" / f"{sym}.parquet"
    funding_file = data_path / "funding" / f"{sym}.parquet"
    metrics_file = data_path / "metrics_5m" / f"{sym}.parquet"

    if not kline_file.exists():
        raise FileNotFoundError(f"Missing klines file: {kline_file}")
    if not funding_file.exists():
        raise FileNotFoundError(f"Missing funding file: {funding_file}")
    if not metrics_file.exists():
        raise FileNotFoundError(f"Missing metrics file: {metrics_file}")

    klines = pd.read_parquet(kline_file)
    funding = pd.read_parquet(funding_file)
    metrics = pd.read_parquet(metrics_file)

    if split == "dev":
        cutoff_ms = get_dev_cutoff_ms()
        klines = klines[klines["open_time"] < cutoff_ms].copy()
        funding = funding[funding["calc_time"] < cutoff_ms].copy()
        metrics = metrics[metrics["ts"] < cutoff_ms].copy()

    return {
        "klines": klines,
        "funding": funding,
        "metrics": metrics,
    }
