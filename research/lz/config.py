"""Configuration and constants for Lorentzian v2 research."""

DEV_ASSETS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT"]
HOLDOUT_ASSET = "SOLUSDT"
ALL_ASSETS = DEV_ASSETS + [HOLDOUT_ASSET]
BAR_MS = 4 * 3600 * 1000
KLINE_START = "2021-09-01"        # 预热用
FEATURE_START = "2021-12-01"      # metrics 起点
DEV_SCORE_START = "2023-01-01"
DEV_END = "2025-01-01"            # 开发数据硬截断（不含）
HOLDOUT_END = "2026-10-01"        # 不含
FEE_BPS = 5.0
SLIP_BPS = {"BTCUSDT": 2.0, "ETHUSDT": 2.0}   # 其他币 4.0
DEFAULT_SLIP_BPS = 4.0
HOLD_BARS = 6
LABEL_COST_MULT = 2.0             # |fwd| > 2 * 往返成本
Z_WIN, Z_MIN = 540, 180           # 90 天窗口 / 30 天最少样本
Z_CLIP = 4.0
BANK_MIN_DAYS, BANK_MAX_DAYS = 365, 3 * 365
MATURITY_MS = 7 * BAR_MS          # bank 样本须满足 open_time <= t_open - 28h
TRIAL_CAP = 200
MIN_TRADES = 300
DATA_DIR = "research/data"
OUT_DIR = "research/out"

FEATURE_NAMES = [
    "funding_level",
    "funding_chg_3d",
    "oi_chg_24h",
    "oi_price_div",
    "taker_imb",
    "top_ls",
    "global_ls",
    "rv_7d",
    "vol_anom",
    "d1_trend",
    "rel_btc",
]

GROUPS = {
    "A": ["funding_level", "funding_chg_3d", "oi_chg_24h", "oi_price_div", "taker_imb", "top_ls", "global_ls"],
    "B": ["taker_imb", "rv_7d", "vol_anom"],
    "C": list(FEATURE_NAMES),
    "D": ["funding_level", "funding_chg_3d", "oi_chg_24h", "oi_price_div", "taker_imb", "top_ls", "global_ls", "rv_7d", "vol_anom"],
}


def rt(sym: str) -> float:
    """Round-trip cost: 2 * (fee + slip) / 10000."""
    slip = SLIP_BPS.get(sym, DEFAULT_SLIP_BPS)
    return 2.0 * (FEE_BPS + slip) / 1e4
