import pytest

from budgetapp import db
from budgetapp.store import Store
from budgetapp.web import create_app

# Real Argon2id, just cheap parameters so tests run fast.
FAST_KDF = {"iterations": 1, "lanes": 1, "memory_kib": 64}
PORT = 8765
PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def conn():
    c = db.create()
    yield c
    c.close()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "budget.vault", backup_dir=tmp_path / "backups", kdf=FAST_KDF)
    yield s
    s.lock()


@pytest.fixture
def app(store):
    app = create_app(store, port=PORT)
    app.config.update(TESTING=True, SERVER_NAME=f"127.0.0.1:{PORT}")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client) -> str:
    with client.session_transaction() as session:
        return session.get("csrf", "")


@pytest.fixture
def unlocked_client(client):
    client.get("/setup")
    client.post(
        "/setup",
        data={"passphrase": PASSPHRASE, "confirm": PASSPHRASE, "csrf_token": csrf(client)},
    )
    return client
