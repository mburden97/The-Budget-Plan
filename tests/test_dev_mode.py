from pathlib import Path

import pytest

from budgetapp import cli, planning
from budgetapp.store import Store
from budgetapp.web import create_app
from tests.conftest import FAST_KDF, PORT, csrf


@pytest.fixture
def dev_store(tmp_path):
    s = Store(tmp_path / "data-dev" / "budget.vault", kdf=FAST_KDF)
    yield s
    s.lock()


def _client(store, *, dev):
    app = create_app(store, port=PORT, dev=dev)
    app.config.update(TESTING=True, SERVER_NAME=f"127.0.0.1:{PORT}")
    return app.test_client()


def test_dev_flag_picks_its_own_folder_and_port(monkeypatch):
    monkeypatch.delenv("BUDGET_DATA_DIR", raising=False)
    normal = cli.parse_args([])
    assert (normal.dev, normal.port, normal.data_dir) == (False, 8766, Path("data"))
    dev = cli.parse_args(["--dev"])
    assert (dev.dev, dev.port, dev.data_dir) == (True, 8767, Path("data-dev"))
    chosen = cli.parse_args(["--dev", "--port", "9100", "--data-dir", "elsewhere"])
    assert (chosen.port, chosen.data_dir) == (9100, Path("elsewhere"))


def test_first_dev_run_makes_a_passphrase_less_vault_with_starter_lines(dev_store):
    assert cli.prepare_dev_vault(dev_store)
    assert dev_store.exists and not dev_store.is_unlocked
    assert dev_store.unlock_blank()
    with dev_store.read() as conn:
        names = {i.name for g in planning.grouped(conn) for i in g.items}
    assert {"Groceries", "Rent / mortgage"} <= names
    assert not cli.prepare_dev_vault(dev_store)  # an existing vault is left alone


def test_dev_mode_opens_straight_to_the_app(dev_store):
    cli.prepare_dev_vault(dev_store)
    client = _client(dev_store, dev=True)
    page = client.get("/")
    assert page.status_code == 200 and b"Dev mode" in page.data
    added = client.post(
        "/networth/accounts",
        data={"csrf_token": csrf(client), "name": "Test checking", "type": "cash"},
    )
    assert added.status_code == 302  # forms work in the auto-opened session


def test_outside_dev_mode_the_same_vault_must_get_a_passphrase(dev_store):
    cli.prepare_dev_vault(dev_store)
    response = _client(dev_store, dev=False).get("/")
    assert response.status_code == 302 and "/set-passphrase" in response.headers["Location"]


def test_dev_setup_accepts_a_blank_passphrase(tmp_path):
    store = Store(tmp_path / "budget.vault", kdf=FAST_KDF)
    client = _client(store, dev=True)
    client.get("/setup")
    client.post("/setup", data={"passphrase": "", "confirm": "", "csrf_token": csrf(client)})
    assert store.exists and store.opens_without_passphrase()
    store.lock()

    strict = _client(Store(tmp_path / "other" / "budget.vault", kdf=FAST_KDF), dev=False)
    strict.get("/setup")
    refused = strict.post(
        "/setup", data={"passphrase": "", "confirm": "", "csrf_token": csrf(strict)}
    )
    assert "/setup" in refused.headers["Location"]  # a normal install still needs one
