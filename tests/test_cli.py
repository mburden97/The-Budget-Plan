import os
from pathlib import Path

from budgetapp import cli


def test_a_checkout_keeps_its_data_beside_the_code(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUDGET_DATA_DIR", raising=False)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert cli.default_data_dir() == Path("data")
    assert cli.default_data_dir(dev=True) == Path("data-dev")


def test_an_installed_copy_uses_the_user_data_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no pyproject.toml and no data/: not a checkout
    monkeypatch.delenv("BUDGET_DATA_DIR", raising=False)
    chosen = cli.default_data_dir()
    assert chosen.is_absolute() and chosen == cli.user_data_dir() / "data"
    assert cli.default_data_dir(dev=True) == cli.user_data_dir() / "data-dev"

    (tmp_path / "data").mkdir()  # an existing data/ is a checkout-like folder again
    assert cli.default_data_dir() == Path("data")


def test_the_environment_variable_wins_except_in_dev_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUDGET_DATA_DIR", str(tmp_path / "elsewhere"))
    assert cli.default_data_dir() == tmp_path / "elsewhere"
    # Dev mode never opens the vault that variable points at.
    assert cli.default_data_dir(dev=True) != tmp_path / "elsewhere"


def test_the_user_data_folder_follows_the_platform(tmp_path, monkeypatch):
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        assert cli.user_data_dir() == tmp_path / "Roaming" / "TheBudgetPlan"
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
        assert cli.user_data_dir() == tmp_path / "share" / "thebudgetplan"
