"""Label generation for Lorentzian v2."""

import numpy as np
import pandas as pd

from lz.config import BAR_MS, LABEL_COST_MULT, rt


def compute_labels(df_klines: pd.DataFrame, sym: str) -> pd.Series:
    """Compute 24h forward return labels for 4h candles.

    For bar t:
      entry = open[t+1]
      exit = open[t+7]
      fwd = exit / entry - 1
      y = +1 if fwd > LABEL_COST_MULT * rt(sym)
          -1 if fwd < -LABEL_COST_MULT * rt(sym)
           0 otherwise
    Missing exit open[t+7] yields NaN.
    """
    labels = pd.Series(np.nan, index=df_klines.index, dtype="float64")
    n = len(df_klines)
    if n < 8:
        return labels

    open_arr = df_klines["open"].to_numpy(dtype="float64")
    ot_arr = df_klines["open_time"].to_numpy(dtype="int64")

    entry = open_arr[1:-6]
    exit_px = open_arr[7:]
    valid_entry_time = ot_arr[1:-6] == ot_arr[:-7] + BAR_MS
    valid_exit_time = ot_arr[7:] == ot_arr[:-7] + 7 * BAR_MS
    valid = valid_entry_time & valid_exit_time & (entry > 0)

    fwd = np.full(n - 7, np.nan, dtype="float64")
    fwd[valid] = exit_px[valid] / entry[valid] - 1.0

    cost_threshold = LABEL_COST_MULT * rt(sym)
    y = np.full(n - 7, np.nan, dtype="float64")

    pos_mask = valid & (fwd > cost_threshold)
    neg_mask = valid & (fwd < -cost_threshold)
    zero_mask = valid & ~pos_mask & ~neg_mask

    y[pos_mask] = 1.0
    y[neg_mask] = -1.0
    y[zero_mask] = 0.0

    labels.iloc[: n - 7] = y
    return labels
