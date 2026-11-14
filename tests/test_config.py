import pytest

from trading_agent.config import ConfigError, load_settings


def test_loads_default_settings(project_root):
    s = load_settings(project_root)
    assert s.mode == "paper"
    assert s.db_file == project_root / "data" / "trading_agent.sqlite3"
    assert s.risk.max_open_positions == 5
    assert isinstance(s.risk.max_open_positions, int)


def test_local_override_merges(project_root):
    (project_root / "config" / "settings.local.toml").write_text("[risk]\nmax_daily_loss_usd = 50\n")
    s = load_settings(project_root)
    assert s.risk.max_daily_loss_usd == 50
    assert s.risk.max_position_usd == 2000  # untouched keys survive the merge


def test_live_mode_is_rejected(project_root):
    (project_root / "config" / "settings.local.toml").write_text('[app]\nmode = "live"\n')
    with pytest.raises(ConfigError, match="not allowed"):
        load_settings(project_root)


@pytest.mark.parametrize("bad", ["0", "-5", '"ten"', "true", "2.5"])
def test_bad_risk_limit_is_rejected(project_root, bad):
    (project_root / "config" / "settings.local.toml").write_text(f"[risk]\nmax_open_positions = {bad}\n")
    with pytest.raises(ConfigError):
        load_settings(project_root)


def test_watchlist_and_data_defaults(project_root):
    s = load_settings(project_root)
    assert s.data_feed == "iex"
    assert s.watchlist[0] == "SPY" and len(s.watchlist) == len(set(s.watchlist))


@pytest.mark.parametrize(
    "override, match",
    [
        ('[broker]\npaper_base_url = "https://api.alpaca.markets"\n', "paper"),
        ('[data]\nfeed = "otc"\n', "feed"),
        ('[data]\ndaily_feed = "otc"\n', "daily_feed"),
        ('[watchlist]\nsymbols = []\n', "non-empty"),
        ('[watchlist]\nsymbols = ["AAPL", "not a ticker"]\n', "Invalid symbol"),
        ('[refresh]\ntimeframes = ["1Week"]\n', "timeframes"),
        ('[refresh]\nmin_coverage = 95\n', "fraction"),
        ('[history]\nminute_days = 0\n', "minute_days"),
        ('[backtest]\nposition_cap_pct = 150\n', "position_cap_pct"),
        ('[backtest]\nholdout_start = "soon"\n', "holdout_start"),
    ],
)
def test_bad_broker_and_data_settings_rejected(project_root, override, match):
    (project_root / "config" / "settings.local.toml").write_text(override)
    with pytest.raises(ConfigError, match=match):
        load_settings(project_root)


def test_missing_settings_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path)
