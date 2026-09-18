import io
from datetime import date, timedelta
from decimal import Decimal

from budgetapp import brokerage, documents, goals, imports, loans, planning, transactions
from budgetapp import networth as nw
from tests.conftest import PASSPHRASE, csrf
from tests.fakes import CG_PRICE, CG_SEARCH, YAHOO, make_fetch, yahoo

PAGES = [
    "/",
    "/documents/",
    "/budget/",
    "/budget/actual",
    "/budget/actual?month=2025-12",
    "/budget/subscriptions",
    "/transactions/",
    "/transactions/?show=uncategorized&month=2026-01",
    "/transactions/import",
    "/transactions/bank-categories",
    "/trends/",
    "/trends/?months=24&by=line",
    "/loans/",
    "/brokerage/",
    "/networth/",
    "/goals/",
    "/settings/",
]


def test_all_pages_render(unlocked_client):
    for path in PAGES:
        assert unlocked_client.get(path).status_code == 200, path


def test_csv_import_categorize_and_actuals(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts", data={"csrf_token": token, "name": "Checking", "type": "cash"}
    )
    with store.write() as conn:
        [acct] = nw.list_accounts(conn)
        flexible = planning.list_categories(conn)[2].id
        coffee = planning.add_line_item(
            conn, category_id=flexible, name="Coffee", amount_cents=3000
        )
    csv_bytes = (
        b"Date,Description,Amount\n09/01/2026,PAYROLL ACME,2600.00\n"
        b"09/02/2026,STARBUCKS 123,-4.50\n09/03/2026,STARBUCKS 456,-5.25\n"
    )

    def upload():
        response = unlocked_client.post(
            "/transactions/import",
            data={"csrf_token": token, "account_id": str(acct.id),
                  "file": (io.BytesIO(csv_bytes), "bank.csv")},
            content_type="multipart/form-data",
        )
        return response.headers["Location"]

    mapping_url = upload()
    assert "/transactions/import/" in mapping_url
    page = unlocked_client.get(mapping_url)
    assert page.status_code == 200 and b"PAYROLL ACME" in page.data
    columns = {"csrf_token": token, "date_col": "0", "description_col": "1",
               "amount_col": "2", "date_format": "auto", "action": "import"}
    done = unlocked_client.post(mapping_url, data=columns)
    assert "month=2026-09" in done.headers["Location"]
    assert unlocked_client.get(mapping_url).status_code == 302  # one-time token

    with store.read() as conn:
        txns = transactions.list_transactions(conn, date(2026, 9, 1))
    assert len(txns) == 3
    first = next(t for t in txns if t.description == "STARBUCKS 123")
    unlocked_client.post(
        f"/transactions/{first.id}/categorize",
        data={"csrf_token": token, "category": str(coffee), "remember": "on",
              "pattern": "STARBUCKS", "month": "2026-09"},
    )
    with store.read() as conn:
        result = transactions.budget_vs_actual(conn, date(2026, 9, 1))
    assert {r.item.name: r.actual_cents for g in result.groups for r in g.rows} == {"Coffee": 975}
    assert b"$9.75" in unlocked_client.get("/budget/actual?month=2026-09").data

    unlocked_client.post(upload(), data=columns)  # same file again: all duplicates
    with store.read() as conn:
        assert len(transactions.list_transactions(conn, date(2026, 9, 1))) == 3


def test_goals_and_essential_toggle(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/goals/funds",
        data={"csrf_token": token, "name": "Car insurance", "target": "720",
              "due_date": "2027-03-01", "saved": "120"},
    )
    with store.read() as conn:
        [fund] = goals.list_funds(conn)
        flexible = planning.list_categories(conn)[2]
    assert fund.saved_cents == 12000 and not flexible.essential
    unlocked_client.post(
        f"/budget/categories/{flexible.id}/essential", data={"csrf_token": token, "essential": "1"}
    )
    with store.read() as conn:
        assert planning.list_categories(conn)[2].essential
    assert b"Car insurance" in unlocked_client.get("/goals/").data
    assert b"Car insurance" in unlocked_client.get("/").data


def test_brokerage_refresh_flow(unlocked_client, store, app):
    token = csrf(unlocked_client)
    response = unlocked_client.post(
        "/networth/accounts",
        data={"csrf_token": token, "name": "Taxable", "type": "brokerage", "next": "brokerage"},
    )
    assert response.headers["Location"].endswith("/brokerage/")
    with store.read() as conn:
        [acct] = nw.list_accounts(conn)
    for symbol, shares, source in [("vti", "10", "market"), ("BTC", "0.5", "crypto")]:
        unlocked_client.post(
            "/brokerage/holdings",
            data={"csrf_token": token, "account_id": acct.id, "symbol": symbol,
                  "shares": shares, "asset_class": "us_stock", "quote_source": source},
        )
    app.config["QUOTE_FETCH"] = make_fetch({
        YAHOO + "VTI?": yahoo("300"),
        CG_SEARCH + "BTC": {"coins": [{"id": "bitcoin", "symbol": "BTC", "market_cap_rank": 1}]},
        CG_PRICE + "bitcoin": {"bitcoin": {"usd": 60000}},
    })
    unlocked_client.post("/brokerage/refresh", data={"csrf_token": token})
    with store.read() as conn:
        assert nw.get_account(conn, acct.id).balance_cents == 300000 + 3000000
    page = unlocked_client.get("/brokerage/").data
    assert b"VTI" in page and b"$33,000.00" in page
    assert b'<td class="num">10</td>' in page and b"1E+1" not in page


def test_api_keys_are_masked(unlocked_client):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/settings/api-keys", data={"csrf_token": token, "finnhub_api_key": "abcd1234wxyz9876"}
    )
    page = unlocked_client.get("/settings/").data
    assert b"abcd1234wxyz9876" not in page and b"9876" in page
    unlocked_client.post(
        "/settings/api-keys", data={"csrf_token": token, "clear-finnhub_api_key": "on"}
    )
    assert b"9876" not in unlocked_client.get("/settings/").data


def test_loan_flow(unlocked_client, store):
    token = csrf(unlocked_client)
    response = unlocked_client.post(
        "/loans/",
        data={"csrf_token": token, "name": "Car", "balance": "10,000", "apr": "6",
              "min_payment": "193.33", "payment_day": "15"},
    )
    with store.read() as conn:
        [loan] = loans.list_loans(conn)
    assert response.headers["Location"].endswith(f"/loans/{loan.account_id}")

    page = unlocked_client.get(f"/loans/{loan.account_id}?extra=100")
    assert page.status_code == 200 and b"What if: 100.00 extra" in page.data
    assert b"Avalanche" in unlocked_client.get("/loans/?extra=50").data

    unlocked_client.post(
        f"/loans/{loan.account_id}/payments",
        data={"csrf_token": token, "amount": "193.33", "paid_on": date.today().isoformat()},
    )
    with store.read() as conn:
        assert loans.get_loan(conn, loan.account_id).balance_cents == 1_000_000 - 14333


def test_dashboard_logs_loan_payments_and_transfers(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/loans/",
        data={"csrf_token": token, "name": "Car", "balance": "10,000", "apr": "6",
              "min_payment": "193.33"},
    )
    for name, balance in (("Everyday", "2500"), ("Rainy Day", "0")):
        unlocked_client.post(
            "/networth/accounts",
            data={"csrf_token": token, "name": name, "type": "cash", "balance": balance},
        )
    with store.read() as conn:
        [loan] = loans.list_loans(conn)
        ids = {a.name: a.id for a in nw.list_accounts(conn, types=("cash",))}
    page = unlocked_client.get("/")
    assert b"Log a loan payment" in page.data and b'value="193.33"' in page.data
    assert b"Log a transfer to savings" in page.data

    today = date.today().isoformat()
    response = unlocked_client.post(
        "/log/loan-payment",
        data={"csrf_token": token, "account_id": loan.account_id, "amount": "193.33",
              "paid_on": today},
    )
    assert response.status_code == 302
    unlocked_client.post(
        "/log/transfer",
        data={"csrf_token": token, "to_account_id": ids["Rainy Day"],
              "from_account_id": ids["Everyday"], "amount": "300", "on": today},
    )
    with store.read() as conn:
        assert loans.get_loan(conn, loan.account_id).balance_cents == 1_000_000 - 14333
        assert nw.get_account(conn, ids["Rainy Day"]).balance_cents == 30000
        assert nw.get_account(conn, ids["Everyday"]).balance_cents == 220000


def test_home_page_background_video(unlocked_client, monkeypatch):
    from budgetapp.web import dashboard

    monkeypatch.setattr(dashboard, "_home_video", lambda: "home-bg.mp4")
    page = unlocked_client.get("/").data
    assert b'<body class="has-backdrop">' in page and b'class="backdrop-video"' in page
    assert b"/static/home-bg.mp4" in page and b"muted" in page
    assert b"backdrop-video" not in unlocked_client.get("/budget/").data  # home page only
    monkeypatch.setattr(dashboard, "_home_video", lambda: None)
    page = unlocked_client.get("/").data
    assert b"backdrop-video" not in page and b"has-backdrop" not in page


def test_only_background_videos_may_be_cached():
    from budgetapp.web import cache_control

    assert cache_control("static", "/static/home-bg.mp4") == "private, max-age=604800"
    assert cache_control("static", "/static/clip.WEBM") == "private, max-age=604800"
    assert cache_control("static", "/static/app.css") == "no-store"
    assert cache_control("dashboard.index", "/") == "no-store"
    assert cache_control("budget.index", "/budget/x.mp4") == "no-store"


def test_the_cat_can_be_turned_off(unlocked_client):
    page = unlocked_client.get("/budget/").data
    assert b'class="cat"' in page and b"cat.js" in page and b"has-cat" in page
    unlocked_client.post("/settings/cat", data={"csrf_token": csrf(unlocked_client)})
    page = unlocked_client.get("/budget/").data
    assert b'class="cat"' not in page and b"cat.js" not in page
    assert b'name="show_cat" >' in unlocked_client.get("/settings/").data  # unchecked
    unlocked_client.post(
        "/settings/cat", data={"csrf_token": csrf(unlocked_client), "show_cat": "on"}
    )
    assert b'class="cat"' in unlocked_client.get("/").data
    assert unlocked_client.get("/static/cat.js").status_code == 200


def test_paying_off_a_loan_rains_money(unlocked_client, store):
    token = csrf(unlocked_client)
    for name, balance in (("Small", "100"), ("Big", "900"), ("Third", "50")):
        unlocked_client.post(
            "/loans/",
            data={"csrf_token": token, "name": name, "balance": balance, "apr": "0",
                  "min_payment": "50"},
        )
    with store.read() as conn:
        ids = {loan.name: loan.account_id for loan in loans.list_loans(conn)}
    today = date.today().isoformat()
    pay = {"csrf_token": token, "account_id": ids["Small"], "paid_on": today}

    unlocked_client.post("/log/loan-payment", data=pay | {"amount": "40"})
    assert b"flash-celebrate" not in unlocked_client.get("/").data  # still owes $60
    unlocked_client.post("/log/loan-payment", data=pay | {"amount": "60"})
    page = unlocked_client.get("/").data
    assert b"flash-celebrate" in page and b"flash-celebrate-big" not in page

    unlocked_client.post(
        f"/loans/{ids['Big']}/payments",
        data={"csrf_token": token, "amount": "900", "paid_on": today},
    )
    assert b"flash-celebrate" in unlocked_client.get(f"/loans/{ids['Big']}").data
    unlocked_client.post(
        "/networth/checkin",
        data={"csrf_token": token, f"balance-{ids['Third']}": "0", "as_of": today},
    )
    page = unlocked_client.get("/networth/").data
    assert b"flash-celebrate-big" in page and b"debt-free" in page  # the last one
    for path in ("/", "/loans/", f"/loans/{ids['Big']}"):
        assert unlocked_client.get(path).status_code == 200  # $0 loans still render
    assert b"rainMoney" in unlocked_client.get("/static/app.js").data


def test_transfer_for_a_sinking_fund(unlocked_client, store):
    with store.write() as conn:
        everyday = nw.add_account(conn, name="Everyday", type="cash")
        rainy = nw.add_account(conn, name="Rainy Day", type="cash", emergency_fund=True)
        nw.record_balance(conn, everyday, 100000)
        fund = goals.add_fund(conn, name="Tires", target_cents=80000)
    page = unlocked_client.get("/").data
    assert b'name="fund_id"' in page and b"Tires (sinking fund)" in page

    base = {"csrf_token": csrf(unlocked_client), "on": date.today().isoformat(),
            "fund_id": fund}
    into = base | {"to_account_id": rainy, "from_account_id": everyday}
    out = base | {"to_account_id": everyday, "from_account_id": rainy}
    unlocked_client.post("/log/transfer", data=into | {"amount": "150"})
    unlocked_client.post("/log/transfer", data=out | {"amount": "40"})  # paying the bill
    unlocked_client.post("/log/transfer", data=out | {"amount": "500"})  # more than saved
    with store.read() as conn:
        assert goals.list_funds(conn)[0].saved_cents == 11000
        assert nw.get_account(conn, rainy).balance_cents == 11000  # the refused one undone
        ef = goals.emergency_fund(conn)
    assert (ef.held_cents, ef.set_aside_cents, ef.saved_cents) == (11000, 11000, 0)


def test_networth_add_account_and_checkin(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts",
        data={"csrf_token": token, "name": "Checking", "type": "cash", "balance": "2500"},
    )
    unlocked_client.post(
        "/networth/accounts", data={"csrf_token": token, "name": "Sneaky", "type": "loan"}
    )
    with store.read() as conn:
        [acct] = nw.list_accounts(conn)
    unlocked_client.post(
        "/networth/checkin",
        data={"csrf_token": token, f"balance-{acct.id}": "3,000",
              "as_of": date.today().isoformat()},
    )
    with store.read() as conn:
        assert nw.net_worth(conn).net_cents == 300000
    assert unlocked_client.get(f"/networth/accounts/{acct.id}").status_code == 200
    assert unlocked_client.get("/networth/accounts/9999").status_code == 404


def test_change_passphrase(unlocked_client, store):
    token = csrf(unlocked_client)
    new = "a different passphrase"
    unlocked_client.post(
        "/settings/passphrase",
        data={"csrf_token": token, "current": "wrong", "new": new, "confirm": new},
    )
    assert store.verify_passphrase(PASSPHRASE)
    unlocked_client.post(
        "/settings/passphrase",
        data={"csrf_token": token, "current": PASSPHRASE, "new": new, "confirm": new},
    )
    assert store.verify_passphrase(new) and not store.verify_passphrase(PASSPHRASE)


def test_first_run_redirects_to_setup(client):
    response = client.get("/budget/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup")


def test_setup_then_budget_page(unlocked_client, store):
    assert store.vault_path.exists()
    response = unlocked_client.get("/budget/")
    assert response.status_code == 200
    assert b"Left to assign" in response.data


def test_foreign_host_rejected(unlocked_client):
    # DNS-rebinding: attacker's domain resolving to 127.0.0.1
    response = unlocked_client.get("/budget/", headers={"Host": "evil.example:8765"})
    assert response.status_code == 400


def test_post_without_csrf_rejected(unlocked_client):
    response = unlocked_client.post("/budget/items", data={"name": "x", "amount": "1"})
    assert response.status_code == 400


def test_cross_origin_post_rejected(unlocked_client):
    response = unlocked_client.post(
        "/budget/items",
        data={"csrf_token": csrf(unlocked_client)},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_browser_form_posts_are_accepted(unlocked_client):
    # Browsers attach Origin to form posts; it must match the app's own origin.
    # (With Referrer-Policy: no-referrer they send "Origin: null", which is rejected;
    # that is why the policy is same-origin.)
    token = csrf(unlocked_client)
    data = {"csrf_token": token, "name": "Checking", "type": "cash"}
    ok = unlocked_client.post(
        "/networth/accounts", data=data, headers={"Origin": "http://127.0.0.1:8765"}
    )
    assert ok.status_code == 302
    null = unlocked_client.post("/networth/accounts", data=data, headers={"Origin": "null"})
    assert null.status_code == 403
    page = unlocked_client.get("/networth/")
    assert page.headers["Referrer-Policy"] == "same-origin"
    assert b'content="no-referrer"' not in page.data


def test_security_headers(client):
    response = client.get("/setup")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_add_edit_delete_line_item(unlocked_client, store):
    with store.read() as conn:
        fixed = planning.list_categories(conn)[1].id
    token = csrf(unlocked_client)

    unlocked_client.post(
        "/budget/items",
        data={"csrf_token": token, "category_id": fixed, "name": "Rent", "amount": "$1,500",
              "frequency": "monthly"},
    )
    with store.read() as conn:
        [item] = [i for g in planning.grouped(conn) for i in g.items]
    assert item.amount_cents == 150000

    unlocked_client.post(
        f"/budget/items/{item.id}",
        data={"csrf_token": token, "category_id": fixed, "name": "Rent", "amount": "1600",
              "frequency": "monthly"},
    )
    page = unlocked_client.get("/budget/").data
    assert b"$1,600.00" in page

    unlocked_client.post(f"/budget/items/{item.id}/delete", data={"csrf_token": token})
    with store.read() as conn:
        assert all(not g.items for g in planning.grouped(conn))


def test_invalid_amount_shows_error(unlocked_client):
    unlocked_client.post(
        "/budget/items",
        data={"csrf_token": csrf(unlocked_client), "category_id": 1, "name": "Pay",
              "amount": "lots"},
    )
    assert b"Amount must be a number" in unlocked_client.get("/budget/").data


def test_lock_and_unlock(unlocked_client):
    unlocked_client.post("/lock", data={"csrf_token": csrf(unlocked_client)})
    assert unlocked_client.get("/budget/").headers["Location"].endswith("/unlock")

    unlocked_client.get("/unlock")
    bad = unlocked_client.post(
        "/unlock", data={"passphrase": "wrong", "csrf_token": csrf(unlocked_client)}
    )
    assert bad.headers["Location"].endswith("/unlock")

    good = unlocked_client.post(
        "/unlock", data={"passphrase": PASSPHRASE, "csrf_token": csrf(unlocked_client)}
    )
    assert good.headers["Location"] in ("/", "http://127.0.0.1:8765/")
    assert unlocked_client.get("/budget/").status_code == 200



def test_account_apy_and_card_flip_suggestion(unlocked_client, store):
    token = csrf(unlocked_client)
    for name, kind in [("HYSA", "cash"), ("Travel card", "credit_card")]:
        unlocked_client.post(
            "/networth/accounts",
            data={"csrf_token": token, "name": name, "type": kind, "balance": "3900"},
        )
    with store.read() as conn:
        accounts = {a.name: a.id for a in nw.list_accounts(conn)}
    unlocked_client.post(
        f"/networth/accounts/{accounts['HYSA']}",
        data={"csrf_token": token, "name": "HYSA", "institution": "Example Bank",
              "notes": "", "include_in_net_worth": "on", "emergency_fund": "on", "apy": "3.0"},
    )
    with store.read() as conn:
        assert nw.get_account(conn, accounts["HYSA"]).apy == Decimal("0.03")
    assert b"3.0% APY" in unlocked_client.get("/networth/").data
    assert b"a month" in unlocked_client.get(f"/networth/accounts/{accounts['HYSA']}").data

    amex = b"Date,Description,Amount\n09/08/2026,GROCER 12,64.20\n09/06/2026,AIRLINE TIX,245.00\n"
    upload = unlocked_client.post(
        "/transactions/import",
        data={"csrf_token": token, "account_id": str(accounts["Travel card"]),
              "file": (io.BytesIO(amex), "activity.csv")},
        content_type="multipart/form-data",
    )
    page = unlocked_client.get(upload.headers["Location"]).data
    assert b'name="flip_sign" checked' in page and b"Flip signs</strong>" in page


def test_bank_categories_flow(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts",
        data={"csrf_token": token, "name": "Rewards card", "type": "credit_card"},
    )
    with store.write() as conn:
        [acct] = nw.list_accounts(conn)
        flexible = planning.list_categories(conn)[2].id
        gas = planning.add_line_item(conn, category_id=flexible, name="Gas", amount_cents=10000)
    card = (
        b"Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        b"09/07/2026,09/08/2026,SHELL OIL,Gas,Sale,-41.20,\n"
        b"09/05/2026,09/06/2026,Payment Thank You,,Payment,500.00,\n"
    )
    upload = unlocked_client.post(
        "/transactions/import",
        data={"csrf_token": token, "account_id": str(acct.id),
              "file": (io.BytesIO(card), "visa.csv")},
        content_type="multipart/form-data",
    )
    mapping_url = upload.headers["Location"]
    assert b'<option value="3" selected>Category</option>' in unlocked_client.get(mapping_url).data
    unlocked_client.post(
        mapping_url,
        data={"csrf_token": token, "date_col": "0", "description_col": "2", "amount_col": "5",
              "category_col": "3", "date_format": "auto", "action": "import"},
    )

    page = unlocked_client.get("/transactions/bank-categories").data
    assert b"Gas" in page and b"suggested" in page
    unlocked_client.post(
        "/transactions/bank-categories",
        data={"csrf_token": token, "name-1": "Gas", "map-1": str(gas)},
    )
    with store.read() as conn:
        [shell] = [
            t for t in transactions.list_transactions(conn, date(2026, 9, 1))
            if t.description == "SHELL OIL"
        ]
    assert (shell.line_item_id, shell.bank_category) == (gas, "Gas")
    assert b"saved" in unlocked_client.get("/transactions/bank-categories").data


def test_split_page_and_split_rule(unlocked_client, store):
    token = csrf(unlocked_client)
    with store.write() as conn:
        cats = planning.list_categories(conn)
        phone = planning.add_line_item(conn, category_id=cats[1].id, name="Phone", amount_cents=1)
        plan = planning.add_line_item(
            conn, category_id=cats[2].id, name="Subscriptions", amount_cents=2000
        )
        bill = transactions.add_transaction(
            conn, posted_on=date(2026, 8, 26), description="PHONECO*BILL QWERTY1",
            amount_cents=-8025, line_item_id=phone,
        )
        older = transactions.add_transaction(
            conn, posted_on=date(2026, 7, 26), description="PHONECO*BILL ZXCVB2",
            amount_cents=-6410, line_item_id=phone,
        )
    page = unlocked_client.get(f"/transactions/{bill}/split?month=2026-08")
    assert page.status_code == 200 and b"PHONECO*BILL QWERTY1" in page.data

    unlocked_client.post(
        f"/transactions/{bill}/splits",
        data={"csrf_token": token, "line_item_id": str(plan), "amount": "15",
              "note": "Cloud storage", "every": "on", "pattern": "PHONECO*BILL",
              "month": "2026-08"},
    )
    with store.read() as conn:
        assert [s.amount_cents for s in transactions.list_splits(conn, bill)] == [-1500]
        [old_split] = transactions.list_splits(conn, older)
    listing = unlocked_client.get("/transactions/?month=2026-08").data
    assert b"Cloud storage" in listing and b"$15.00 of each" in listing
    assert b"Cloud storage" in unlocked_client.get("/budget/subscriptions").data

    unlocked_client.post(
        f"/transactions/splits/{old_split.id}/delete",
        data={"csrf_token": token, "month": "2026-07"},
    )
    with store.read() as conn:
        assert transactions.list_splits(conn, older) == []
    assert unlocked_client.get("/transactions/999999/split").status_code == 404


def test_subscriptions_tab(unlocked_client, store):
    from datetime import timedelta

    today = date.today()
    with store.write() as conn:
        flexible = planning.list_categories(conn)[2].id
        line = planning.add_line_item(
            conn, category_id=flexible, name="Subscriptions", amount_cents=2000
        )
        fun = planning.add_line_item(conn, category_id=flexible, name="Fun", amount_cents=1)
        for back in (5, 35, 65):
            day = today - timedelta(days=back)
            transactions.add_transaction(
                conn, posted_on=day, description="NETFLIX.COM", amount_cents=-1549,
                line_item_id=line,
            )
            transactions.add_transaction(
                conn, posted_on=day, description="TUNESTREAM MUSIC", amount_cents=-1099,
                line_item_id=fun,
            )
    page = unlocked_client.get("/budget/subscriptions").data
    assert b"Netflix.com" in page and b"$15.49" in page and b"Monthly" in page
    assert b"Possible subscriptions" in page and b"Tunestream Music" in page

    unlocked_client.post(
        "/budget/subscriptions/adopt",
        data={"csrf_token": csrf(unlocked_client), "pattern": "TUNESTREAM MUSIC"},
    )
    with store.read() as conn:
        moved = conn.execute(
            "SELECT line_item_id FROM transactions WHERE description = 'TUNESTREAM MUSIC'"
        ).fetchall()
    assert len(moved) == 3 and all(row[0] == line for row in moved)
    page = unlocked_client.get("/budget/subscriptions").data
    assert b"Possible subscriptions" not in page and b"$26.48" in page  # both monthly

    unlocked_client.post(
        "/budget/subscriptions/release",
        data={"csrf_token": csrf(unlocked_client), "pattern": "TUNESTREAM MUSIC"},
    )
    with store.read() as conn:
        lines = {row[0] for row in conn.execute(
            "SELECT line_item_id FROM transactions WHERE description = 'TUNESTREAM MUSIC'"
        )}
    assert lines == {fun}  # back to the only non-subscription expense line
    assert b"$15.49" in unlocked_client.get("/budget/subscriptions").data


def test_cancelled_subscriptions(unlocked_client, store):
    from datetime import timedelta

    today = date.today()
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts", data={"csrf_token": token, "name": "Everyday", "type": "cash"}
    )
    with store.write() as conn:
        [acct] = nw.list_accounts(conn)
        flexible = planning.list_categories(conn)[2].id
        line = planning.add_line_item(
            conn, category_id=flexible, name="Subscriptions", amount_cents=2000
        )
        for back in (12, 42, 72):
            transactions.add_transaction(
                conn, posted_on=today - timedelta(days=back), description="GYMCO CLUB",
                amount_cents=-4500, line_item_id=line,
            )
    cancel = {"csrf_token": token, "pattern": "GYMCO CLUB", "name": "Gymco Club"}
    earlier = (today - timedelta(days=5)).isoformat()
    unlocked_client.post("/budget/subscriptions/cancel", data=cancel | {"on": earlier})
    page = unlocked_client.get("/budget/subscriptions").data
    assert b"<h2>Cancelled</h2>" in page and b"No active subscriptions found" in page
    assert b"Charged after you cancelled" not in page

    csv_bytes = f"Date,Description,Amount\n{today:%m/%d/%Y},GYMCO CLUB,-45.00\n".encode()
    mapping_url = unlocked_client.post(
        "/transactions/import",
        data={"csrf_token": token, "account_id": str(acct.id),
              "file": (io.BytesIO(csv_bytes), "bank.csv")},
        content_type="multipart/form-data",
    ).headers["Location"]
    done = unlocked_client.post(
        mapping_url,
        data={"csrf_token": token, "date_col": "0", "description_col": "1", "amount_col": "2",
              "date_format": "auto", "action": "import"},
    )
    after_import = unlocked_client.get(done.headers["Location"]).data
    assert b"from a subscription you cancelled" in after_import
    assert b"Charged after you cancelled" in unlocked_client.get("/budget/subscriptions").data
    assert b"from subscriptions you cancelled" in unlocked_client.get("/").data

    unlocked_client.post("/budget/subscriptions/cancel", data=cancel | {"on": today.isoformat()})
    assert b"Charged after you cancelled" not in unlocked_client.get("/budget/subscriptions").data
    unlocked_client.post(
        "/budget/subscriptions/uncancel", data={"csrf_token": token, "pattern": "GYMCO CLUB"}
    )
    page = unlocked_client.get("/budget/subscriptions").data
    assert b"<h2>Cancelled</h2>" not in page and b"No active subscriptions found" not in page


def test_promo_deadline_warning(unlocked_client, store):
    from budgetapp.dates import add_months

    token = csrf(unlocked_client)
    ends = add_months(date.today(), 10).isoformat()
    loan = {"csrf_token": token, "name": "Store card", "balance": "5000", "apr": "0",
            "min_payment": "100", "payment_day": "1", "promo_ends_on": ends}
    unlocked_client.post("/loans/", data=loan)
    with store.read() as conn:
        [created] = loans.list_loans(conn)
    assert created.promo_ends_on.isoformat() == ends
    home = unlocked_client.get("/").data
    assert b"before the promo ends" in home  # $100/mo won't clear $5,000 in 10 months
    page = unlocked_client.get(f"/loans/{created.account_id}").data
    assert b"Promo ends" in page and f'value="{ends}"'.encode() in page

    unlocked_client.post(
        f"/loans/{created.account_id}", data=loan | {"extra_payment": "1000"}
    )
    assert b"before the promo ends" not in unlocked_client.get("/").data  # $1,100/mo does
    assert unlocked_client.get("/loans/").status_code == 200


def test_base_income_in_settings(unlocked_client, store):
    with store.write() as conn:
        income = next(c.id for c in planning.list_categories(conn) if c.kind == "income")
        pay = planning.add_line_item(
            conn, category_id=income, name="Pay", amount_cents=150000, frequency="biweekly"
        )
    page = unlocked_client.get("/settings/").data
    assert b"Base income" in page and f'name="amount-{pay}" value="1500.00"'.encode() in page
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/settings/income",
        data={"csrf_token": token, f"amount-{pay}": "1,625.50", f"frequency-{pay}": "semimonthly"},
    )
    with store.read() as conn:
        [item] = [i for g in planning.grouped(conn) for i in g.items if i.id == pay]
    assert (item.amount_cents, item.frequency, item.name) == (162550, "semimonthly", "Pay")
    assert b"Left to assign changed" in unlocked_client.get("/settings/").data

    unlocked_client.post("/settings/income", data={"csrf_token": token, f"amount-{pay}": "lots"})
    assert b"Pay must be a number" in unlocked_client.get("/settings/").data
    with store.read() as conn:
        [item] = [i for g in planning.grouped(conn) for i in g.items if i.id == pay]
    assert item.amount_cents == 162550  # the bad value changed nothing


def test_trends_projection_section(unlocked_client):
    assert b"Projected net worth" in unlocked_client.get("/trends/").data
    page = unlocked_client.get("/trends/?years=5&growth=7&p=1&months=6").data
    assert b"7.0% a year" in page and b'<option value="5" selected>' in page
    assert b"months=24" in page and b"growth=7" in page  # range pills keep the settings
    bad = unlocked_client.get("/trends/?growth=lots&p=1").data
    assert b"Investment growth must be a number" in bad and b"Projected net worth" in bad
    assert b"use 30% a year or less" in unlocked_client.get("/trends/?growth=45&p=1").data


def test_trends_page(unlocked_client, store):
    from budgetapp.dates import add_months, month_start

    last_month = add_months(month_start(date.today()), -1)
    with store.write() as conn:
        flexible = planning.list_categories(conn)[2].id
        coffee = planning.add_line_item(
            conn, category_id=flexible, name="Coffee", amount_cents=3000
        )
        transactions.add_transaction(
            conn, posted_on=last_month.replace(day=5), description="STARBUCKS",
            amount_cents=-1234, line_item_id=coffee,
        )
        transactions.add_transaction(
            conn, posted_on=date.today(), description="THIS MONTH", amount_cents=-99999,
            line_item_id=coffee,
        )
    page = unlocked_client.get("/trends/?months=6&by=line").data
    assert b"Coffee" in page and b"<svg" in page and b'class="series-0"' in page
    assert b"$12.34" in page and b"$999.99" not in page  # current partial month left out
    assert b"Flexible Expenses" in unlocked_client.get("/trends/").data


def test_show_archived_accounts(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts",
        data={"csrf_token": token, "name": "Old Bank", "type": "cash", "balance": "25"},
    )
    assert b"Show archived" not in unlocked_client.get("/networth/").data
    with store.write() as conn:
        [acct] = nw.list_accounts(conn)
        nw.update_account(
            conn, acct.id, name="Old Bank", institution="", notes="",
            include_in_net_worth=True, emergency_fund=False, archived=True,
        )
    page = unlocked_client.get("/networth/").data
    assert b"Old Bank" not in page and b"Show archived (1)" in page
    assert b"$25.00" in page  # still counted in net worth
    page = unlocked_client.get("/networth/?archived=1").data
    assert b"Old Bank" in page and b">archived<" in page and b"Hide archived" in page
    assert b"Untick" in unlocked_client.get(f"/networth/accounts/{acct.id}").data


def test_budget_mode_setting(unlocked_client, store):
    token = csrf(unlocked_client)
    with store.write() as conn:
        income = planning.list_categories(conn)[0].id
        planning.add_line_item(
            conn, category_id=income, name="Pay", amount_cents=200000, frequency="biweekly"
        )
    page = unlocked_client.get("/budget/").data  # two-paycheck is the default
    assert b"$4,000.00" in page and b"3rd-paycheck windfalls" in page

    form = {"csrf_token": token, "budget_mode": "average"}
    unlocked_client.post("/settings/budget-mode", data=form)
    page = unlocked_client.get("/budget/").data
    assert b"$4,333.33" in page and b"3rd-paycheck windfalls" not in page


def test_blank_passphrases_are_refused(client, store):
    client.get("/setup")
    client.post("/setup", data={"passphrase": "", "confirm": "", "csrf_token": csrf(client)})
    assert not store.exists
    assert b"Use at least" in client.get("/setup").data


def test_vault_without_passphrase_must_set_one(client, app, store):
    store.create("")  # made in the old no-passphrase mode
    store.lock()
    assert client.get("/budget/").headers["Location"].endswith("/set-passphrase")
    assert client.get("/unlock").headers["Location"].endswith("/set-passphrase")
    page = client.get("/set-passphrase")
    assert page.status_code == 200 and b"no reset or recovery" in page.data
    form = {"csrf_token": csrf(client)}
    client.post("/set-passphrase", data=form | {"passphrase": "short", "confirm": "short"})
    assert b"Use at least" in client.get("/set-passphrase").data
    client.post(
        "/set-passphrase", data=form | {"passphrase": PASSPHRASE, "confirm": PASSPHRASE + "x"}
    )
    assert b"match" in client.get("/set-passphrase").data
    assert store.opens_without_passphrase()  # nothing changed yet

    done = client.post(
        "/set-passphrase", data=form | {"passphrase": PASSPHRASE, "confirm": PASSPHRASE}
    )
    assert done.headers["Location"].endswith("/settings/#recovery")  # next: a recovery code
    assert client.get("/budget/").status_code == 200
    assert store.verify_passphrase(PASSPHRASE) and not store.passphrase_blank
    assert not list(store.backup_dir.glob("*.vault"))  # no new unprotected backup
    assert app.test_client().get("/budget/").headers["Location"].endswith("/unlock")
    store.lock()
    assert not store.opens_without_passphrase()
    assert client.get("/set-passphrase").headers["Location"].endswith("/unlock")


def test_passphrase_cannot_be_removed(unlocked_client, store):
    unlocked_client.post(
        "/settings/passphrase",
        data={"csrf_token": csrf(unlocked_client), "current": PASSPHRASE, "new": "",
              "confirm": ""},
    )
    assert store.verify_passphrase(PASSPHRASE) and not store.passphrase_blank
    assert b"Use at least" in unlocked_client.get("/settings/").data

def test_setup_still_rejects_short_or_mismatched(client, store):
    client.get("/setup")
    for pw, confirm in [("short", "short"), ("long enough phrase", "different phrase")]:
        form = {"passphrase": pw, "confirm": confirm, "csrf_token": csrf(client)}
        client.post("/setup", data=form)
    assert not store.exists


def test_other_client_cannot_use_unlocked_vault(unlocked_client, app):
    # A second browser/process without the session cookie is sent to unlock.
    stranger = app.test_client()
    assert stranger.get("/budget/").headers["Location"].endswith("/unlock")


def test_recovery_code_flow(unlocked_client, store):
    import re

    token = csrf(unlocked_client)
    assert b"No recovery code yet" in unlocked_client.get("/settings/").data
    assert b"No recovery code yet" in unlocked_client.get("/").data
    wrong = unlocked_client.post(
        "/settings/recovery-code", data={"csrf_token": token, "current": "not it"}
    )
    assert wrong.status_code == 302 and not store.has_recovery_code
    shown = unlocked_client.post(
        "/settings/recovery-code", data={"csrf_token": token, "current": PASSPHRASE}
    )
    assert shown.status_code == 200 and shown.headers["Cache-Control"] == "no-store"
    code = re.search(rb'class="recovery-code">([A-Z2-7-]+)<', shown.data).group(1).decode()
    with unlocked_client.session_transaction() as session:
        assert code not in repr(dict(session))  # never stored in the cookie
    assert store.has_recovery_code
    assert b"A recovery code is set" in unlocked_client.get("/settings/").data

    unlocked_client.post("/lock", data={"csrf_token": token})
    assert b"Use your recovery code" in unlocked_client.get("/unlock").data
    unlocked_client.get("/recover")
    token = csrf(unlocked_client)
    unlocked_client.post("/recover", data={"csrf_token": token, "code": "NOPE-NOPE-NOPE"})
    assert b"open this vault" in unlocked_client.get("/recover").data
    done = unlocked_client.post("/recover", data={"csrf_token": token, "code": code.lower()})
    assert done.headers["Location"].endswith("/recover/new-passphrase")
    assert unlocked_client.get("/budget/").headers["Location"].endswith("/recover/new-passphrase")

    token = csrf(unlocked_client)
    new = "a whole new passphrase"
    unlocked_client.post(
        "/recover/new-passphrase", data={"csrf_token": token, "passphrase": new, "confirm": "x"}
    )
    assert b"match" in unlocked_client.get("/recover/new-passphrase").data
    unlocked_client.post(
        "/recover/new-passphrase", data={"csrf_token": token, "passphrase": new, "confirm": new}
    )
    assert unlocked_client.get("/budget/").status_code == 200
    assert store.verify_passphrase(new) and not store.verify_passphrase(PASSPHRASE)
    assert store.has_recovery_code  # the code still works

    unlocked_client.post(
        "/settings/recovery-code/remove", data={"csrf_token": token, "current": new}
    )
    assert not store.has_recovery_code


def test_dividend_fields_on_the_brokerage_pages(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts", data={"csrf_token": token, "name": "Taxable", "type": "brokerage"}
    )
    with store.read() as conn:
        [account] = nw.list_accounts(conn)
    unlocked_client.post(
        "/brokerage/holdings",
        data={"csrf_token": token, "account_id": account.id, "symbol": "DRIP", "shares": "100",
              "quote_source": "manual", "price": "10", "dividend_yield": "4", "reinvest": "1"},
    )
    page = unlocked_client.get("/brokerage/").data
    assert b"Dividends / year" in page and b"$40.00" in page and b"reinvested" in page
    with store.read() as conn:
        [holding] = brokerage.list_holdings(conn)
    assert holding.dividend_yield == Decimal("0.04") and holding.reinvest

    unlocked_client.post(  # untick reinvest: now it is cash
        f"/brokerage/holdings/{holding.id}",
        data={"csrf_token": token, "symbol": "DRIP", "shares": "100", "asset_class": "us_stock",
              "name": "", "quote_source": "manual", "quote_id": "", "dividend_yield": "4"},
    )
    with store.read() as conn:
        [holding] = brokerage.list_holdings(conn)
    assert not holding.reinvest
    assert b"as cash" in unlocked_client.get("/brokerage/").data
    bad = unlocked_client.post(
        f"/brokerage/holdings/{holding.id}",
        data={"csrf_token": token, "symbol": "DRIP", "shares": "100", "asset_class": "us_stock",
              "name": "", "quote_source": "manual", "quote_id": "", "dividend_yield": "40"},
    )
    assert bad.status_code in (302, 200)
    with store.read() as conn:
        assert brokerage.list_holdings(conn)[0].dividend_yield == Decimal("0.04")  # unchanged


def test_refreshing_dividend_yields(unlocked_client, store, app):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts", data={"csrf_token": token, "name": "Taxable", "type": "brokerage"}
    )
    with store.read() as conn:
        [account] = nw.list_accounts(conn)
    for symbol, yield_ in (("PAYS", ""), ("MINE", "9")):
        unlocked_client.post(
            "/brokerage/holdings",
            data={"csrf_token": token, "account_id": account.id, "symbol": symbol,
                  "shares": "10", "quote_source": "market", "dividend_yield": yield_},
        )
    chart = {"chart": {"result": [{"meta": {"regularMarketPrice": 100},
                                   "events": {"dividends": {"1": {"date": 1, "amount": "2.00"}}}}]}}
    app.config["QUOTE_FETCH"] = make_fetch({
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{s}?range=1y&interval=1mo&events=div%2Csplit": chart for s in ("PAYS", "MINE")
    })

    unlocked_client.post("/brokerage/dividends/refresh", data={"csrf_token": token})
    with store.read() as conn:
        found = {h.symbol: (h.dividend_yield, h.dividend_source) for h in
                 brokerage.list_holdings(conn)}
    assert found["PAYS"] == (Decimal("0.02"), "Yahoo")
    assert found["MINE"] == (Decimal("0.09"), "manual")  # what the user typed stands

    unlocked_client.post(
        "/brokerage/dividends/refresh", data={"csrf_token": token, "replace": "1"}
    )
    with store.read() as conn:
        assert brokerage.list_holdings(conn)[0].dividend_yield == Decimal("0.02")
    assert b"Yahoo" in unlocked_client.get("/brokerage/").data


def _upload(client, token, content, filename, **fields):
    return client.post(
        "/documents/upload",
        data={"csrf_token": token, "file": (io.BytesIO(content), filename), **fields},
        content_type="multipart/form-data",
    )


def test_documents_upload_new_versions_and_serve_them_back(unlocked_client, store):
    token = csrf(unlocked_client)
    sent = _upload(unlocked_client, token, b"%PDF-1.7 first", "policy.pdf",
                   name="Renters policy")
    assert sent.headers["Location"].endswith("/documents/")
    listed = unlocked_client.get("/documents/").data
    assert b"Renters policy" in listed and b"Last updated" in listed
    assert b"Renters policy" not in unlocked_client.get("/").data  # the home page stays as it was

    with store.read() as conn:
        [doc] = documents.list_documents(conn)
    shown = unlocked_client.get(f"/documents/files/{doc.latest.id}")
    assert shown.data == b"%PDF-1.7 first"
    assert shown.headers["Content-Type"].startswith("application/pdf")
    assert shown.headers["Content-Disposition"].startswith("inline")
    assert "no-store" in shown.headers["Cache-Control"]
    forced = unlocked_client.get(f"/documents/files/{doc.latest.id}?download=1")
    assert forced.headers["Content-Disposition"].startswith("attachment")

    _upload(unlocked_client, token, b"%PDF-1.7 second", "policy-2027.pdf", document_id=str(doc.id))
    page = unlocked_client.get("/documents/").data
    assert b"1 older version" in page and b"policy-2027.pdf" in page
    with store.read() as conn:
        assert len(documents.list_documents(conn)[0].versions) == 2
    assert len(list(store.documents_dir.glob("*.bin"))) == 2

    unlocked_client.post(f"/documents/{doc.id}/delete", data={"csrf_token": token})
    with store.read() as conn:
        assert documents.list_documents(conn) == []
    assert list(store.documents_dir.glob("*.bin")) == []


def test_an_uploaded_page_is_never_served_as_html(unlocked_client, store):
    token = csrf(unlocked_client)
    _upload(unlocked_client, token, b"<script>alert(1)</script>", "sneaky.html", name="Odd one")
    with store.read() as conn:
        [doc] = documents.list_documents(conn)
    served = unlocked_client.get(f"/documents/files/{doc.latest.id}")
    assert served.headers["Content-Type"].startswith("application/octet-stream")
    assert served.headers["Content-Disposition"].startswith("attachment")
    assert served.headers["X-Content-Type-Options"] == "nosniff"


def test_an_upload_without_a_file_or_a_name_is_refused(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post("/documents/upload", data={"csrf_token": token, "name": "Lease"})
    _upload(unlocked_client, token, b"data", "x.pdf")  # no name and no document chosen
    with store.read() as conn:
        assert documents.list_documents(conn) == []
    assert not store.documents_dir.exists() or list(store.documents_dir.glob("*.bin")) == []


def test_a_csv_filed_under_documents_can_be_imported(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts", data={"csrf_token": token, "name": "Checking", "type": "cash"}
    )
    with store.read() as conn:
        [account] = nw.list_accounts(conn)
    rows = (b"Date,Description,Amount\n10/01/2026,PAYROLL ACME,2600.00\n"
            b"10/02/2026,CORNER STORE,-12.40\n")
    _upload(unlocked_client, token, rows, "october.csv", name="Bank statement")
    assert b"Import transactions" in unlocked_client.get("/documents/").data

    with store.read() as conn:
        [doc] = documents.list_documents(conn)
    sent = unlocked_client.post(
        f"/documents/files/{doc.latest.id}/import",
        data={"csrf_token": token, "account_id": str(account.id)},
    )
    mapping_url = sent.headers["Location"]
    assert "/transactions/import/" in mapping_url
    assert b"PAYROLL ACME" in unlocked_client.get(mapping_url).data
    done = unlocked_client.post(mapping_url, data={
        "csrf_token": token, "date_col": "0", "description_col": "1", "amount_col": "2",
        "date_format": "auto", "action": "import",
    })
    assert "month=2026-10" in done.headers["Location"]
    with store.read() as conn:
        assert len(transactions.list_transactions(conn, date(2026, 10, 1))) == 2

    # A PDF has no import button and is refused if the endpoint is called anyway.
    _upload(unlocked_client, token, b"%PDF-1.7 x", "policy.pdf", name="Policy")
    with store.read() as conn:
        pdf = next(d for d in documents.list_documents(conn) if d.name == "Policy")
    refused = unlocked_client.post(
        f"/documents/files/{pdf.latest.id}/import",
        data={"csrf_token": token, "account_id": str(account.id)},
    )
    assert refused.headers["Location"].endswith("/documents/")
    assert b"Only a CSV" in unlocked_client.get("/documents/").data


def test_the_lock_control_is_an_icon(unlocked_client):
    page = unlocked_client.get("/").data
    assert b'aria-label="Lock the vault"' in page and b"<svg" in page


def test_an_ofx_download_imports_without_mapping_and_reimports_exactly(unlocked_client, store):
    from tests.test_ofx import SGML

    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts", data={"csrf_token": token, "name": "Checking", "type": "cash"}
    )
    with store.read() as conn:
        [account] = nw.list_accounts(conn)

    def upload(filename):
        sent = unlocked_client.post(
            "/transactions/import",
            data={"csrf_token": token, "account_id": str(account.id),
                  "file": (io.BytesIO(SGML), filename)},
            content_type="multipart/form-data",
        )
        return sent.headers["Location"]

    mapping_url = upload("september.qfx")
    page = unlocked_client.get(mapping_url).data
    assert b"Read from an OFX file" in page and b"Transaction ID" in page
    assert b"CORNER CAFE &amp; BAKERY SPRINGFIELD" in page  # the preview

    fields = {"csrf_token": token, "date_col": "0", "description_col": "1", "amount_col": "2",
              "id_col": "4", "date_format": "%Y-%m-%d", "action": "import"}
    done = unlocked_client.post(mapping_url, data=fields)
    assert "month=2026-09" in done.headers["Location"]
    with store.read() as conn:
        assert len(transactions.list_transactions(conn, date(2026, 9, 1))) == 3
        assert imports.get_profile(conn, account.id) is None  # a CSV profile isn't overwritten

    unlocked_client.post(upload("september-again.ofx"), data=fields)  # same download twice
    with store.read() as conn:
        assert len(transactions.list_transactions(conn, date(2026, 9, 1))) == 3

    # Filed under Documents, a QFX gets the same Import button as a CSV.
    _upload(unlocked_client, token, SGML, "september.qfx", name="Checking download")
    assert b"Import transactions" in unlocked_client.get("/documents/").data


def test_a_day_first_file_says_so_on_the_preview(unlocked_client, store):
    token = csrf(unlocked_client)
    unlocked_client.post(
        "/networth/accounts", data={"csrf_token": token, "name": "Checking", "type": "cash"}
    )
    with store.read() as conn:
        [account] = nw.list_accounts(conn)
    rows = b"Date,Description,Amount\n03/04/2026,RENT,-900.00\n13/04/2026,GROCER,-45.10\n"
    sent = unlocked_client.post(
        "/transactions/import",
        data={"csrf_token": token, "account_id": str(account.id),
              "file": (io.BytesIO(rows), "april.csv")},
        content_type="multipart/form-data",
    )
    page = unlocked_client.get(sent.headers["Location"]).data
    assert b"read day first" in page and b"2026-04-03" in page


def test_loan_minimum_rules_and_the_promo_flag(unlocked_client, store):
    token = csrf(unlocked_client)
    deadline = (date.today() + timedelta(days=270)).isoformat()
    unlocked_client.post("/loans/", data={
        "csrf_token": token, "name": "Store card", "balance": "6000", "apr": "0",
        "min_rule": "percent", "min_percent": "3", "min_payment": "400",
        "payment_day": "15", "promo_ends_on": deadline,
    })
    unlocked_client.post("/loans/", data={
        "csrf_token": token, "name": "Personal", "balance": "3000", "apr": "10",
        "min_rule": "none", "min_payment": "", "extra_payment": "100",
    })
    with store.read() as conn:
        found = {loan.name: loan for loan in loans.list_loans(conn)}
    assert (found["Store card"].min_rule, found["Store card"].min_percent) == (
        "percent", Decimal("0.03")
    )
    assert (found["Personal"].min_rule, found["Personal"].payment_cents) == ("none", 10000)

    page = unlocked_client.get("/loans/").data
    assert b"of balance" in page and b"no minimum" in page
    # $500 a month can't clear $6,000 in about nine payments, whichever order is chosen.
    assert b"Leaves a balance on Store card when its promo ends" in page

    unlocked_client.post(f"/loans/{found['Personal'].account_id}", data={
        "csrf_token": token, "name": "Personal", "apr": "10", "min_rule": "none",
        "min_payment": "", "extra_payment": "0",
    })  # paying nothing at all is refused
    with store.read() as conn:
        assert loans.get_loan(conn, found["Personal"].account_id).payment_cents == 10000
    detail = unlocked_client.get(f"/loans/{found['Personal'].account_id}").data
    assert b"Current plan" in detail and b"Payment only, no extra" not in detail


def test_interest_saved_is_not_measured_against_a_plan_that_misses_a_promo(unlocked_client):
    token = csrf(unlocked_client)
    deadline = (date.today() + timedelta(days=270)).isoformat()
    # The deadline is 8 or 9 payments away (depending on today). $600 a month on the card
    # falls short of $6,000 either way; with the personal loan's $200 pooled in ($800,
    # against a pace of $667-750) every strategy clears it in time.
    unlocked_client.post("/loans/", data={
        "csrf_token": token, "name": "Store card", "balance": "6000", "apr": "0",
        "min_rule": "percent", "min_percent": "3", "min_payment": "600",
        "payment_day": "15", "promo_ends_on": deadline,
    })
    unlocked_client.post("/loans/", data={
        "csrf_token": token, "name": "Personal", "balance": "2500", "apr": "8",
        "min_rule": "none", "extra_payment": "200",
    })
    page = unlocked_client.get("/loans/").data
    assert page.count(b"Leaves a balance on Store card") == 1  # the current plan only
    assert page.count(b"n/a: current payments miss the promo") == 2  # avalanche, snowball
