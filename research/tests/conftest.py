"""Shared pytest configuration for the research prototypes."""

from pathlib import Path
import pytest

from lz.config import DATA_DIR


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_market_data: reads real downloads under research/data")


def pytest_collection_modifyitems(config, items):
    """Market data is never shipped; without a local download these tests cannot run."""
    if (Path(DATA_DIR) / "klines_4h").is_dir():
        return
    skip = pytest.mark.skip(reason=f"market data not downloaded ({DATA_DIR}/klines_4h); run lz.download")
    for item in items:
        if "needs_market_data" in item.keywords:
            item.add_marker(skip)
