"""Unit tests for labels.py."""

import numpy as np
import pandas as pd
import pytest

from lz.config import BAR_MS, LABEL_COST_MULT, rt
from lz.labels import compute_labels


def test_labels_basic():
    sym = "BTCUSDT"
    cost_threshold = LABEL_COST_MULT * rt(sym)  # 2 * 0.0014 = 0.0028

    # 15 bars
    times = [i * BAR_MS for i in range(15)]
    # Case 1: bar 0 -> entry=open[1]=100, exit=open[7]=101 -> fwd = 0.01 > 0.0028 -> +1
    # Case 2: bar 1 -> entry=open[2]=100, exit=open[8]=99  -> fwd = -0.01 < -0.0028 -> -1
    # Case 3: bar 2 -> entry=open[3]=100, exit=open[9]=100.1 -> fwd = 0.001 within threshold -> 0
    opens = [100.0] * 15
    opens[7] = 101.0
    opens[8] = 99.0
    opens[9] = 100.1

    df = pd.DataFrame({"open_time": times, "open": opens})
    y = compute_labels(df, sym)

    assert y.iloc[0] == 1.0
    assert y.iloc[1] == -1.0
    assert y.iloc[2] == 0.0

    # The last 7 bars must have NaN labels
    for i in range(8, 15):
        assert np.isnan(y.iloc[i])


def test_labels_with_time_gap():
    sym = "BTCUSDT"
    times = [i * BAR_MS for i in range(15)]
    # Introduce gap at bar 7: make it BAR_MS * 8 instead of BAR_MS * 7
    times[7] += BAR_MS
    opens = [100.0] * 15
    opens[7] = 110.0

    df = pd.DataFrame({"open_time": times, "open": opens})
    y = compute_labels(df, sym)

    # bar 0: exit should have been at t+7*BAR_MS, but times[7] has a gap -> NaN
    assert np.isnan(y.iloc[0])
