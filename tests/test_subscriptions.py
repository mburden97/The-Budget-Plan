from datetime import date

import pytest

from budgetapp import planning
from budgetapp import subscriptions as subs
from budgetapp import transactions as tx

TODAY = date(2026, 9, 14)


def _add(conn, day, description, cents, **kwargs):
    return tx.add_transaction(
        conn, posted_on=day, description=description, amount_cents=cents, **kwargs
    )


@pytest.fixture
def lines(conn):
    flexible = {c.name: c.id for c in planning.list_categories(conn)}["Flexible Expenses"]
    return {
        "subs": planning.add_line_item(
            conn, category_id=flexible, name="Subscriptions / Memberships", amount_cents=5000
        ),
        "fun": planning.add_line_item(conn, category_id=flexible, name="Fun", amount_cents=1),
    }


def test_itemize_groups_by_rule_and_works_out_cadence(conn, lines):
    tx.add_rule(conn, pattern="YOUTUBEPREMI", line_item_id=lines["subs"])
    usual, other = "GOOGLE *YOUTUBEPREMIUM", "GOOGLE *YOUTUBEPREMI G.CO/HELPPAY#"
    for month, spelling in [(5, usual), (6, usual), (7, other), (8, usual)]:
        _add(conn, date(2026, month, 3), spelling, -1514)
    tx.apply_rules(conn)
    _add(conn, date(2025, 9, 1), "ADOBE ANNUAL PLAN", -23988, line_item_id=lines["subs"])
    _add(conn, date(2026, 8, 30), "ADOBE ANNUAL PLAN", -23988, line_item_id=lines["subs"])
    _add(conn, date(2026, 1, 10), "AMC THEATRES", -1800, line_item_id=lines["subs"])

    items = {s.name: s for s in subs.itemize(conn)}
    assert set(items) == {"Youtubepremium", "Adobe Annual Plan", "Amc Theatres"}

    youtube = items["Youtubepremium"]  # both spellings, one subscription
    assert (len(youtube.charges), youtube.month_count) == (4, 4)
    assert (youtube.cadence_label, youtube.monthly_cents) == ("Monthly", 1514)
    assert youtube.is_active(TODAY) and youtube.next_due(TODAY) == date(2026, 10, 2)

    adobe = items["Adobe Annual Plan"]
    assert (adobe.cadence_label, adobe.monthly_cents) == ("Yearly", 1999)
    assert adobe.next_due(TODAY) == date(2027, 8, 30)
    assert adobe.spent_since(date(2025, 9, 14)) == 23988

    amc = items["Amc Theatres"]
    assert (amc.cadence_label, amc.monthly_cents, amc.is_active(TODAY)) == ("One-off", None, False)


def test_statement_credit_is_itemized_as_a_negative_cost(conn, lines):
    for month in (6, 7, 8):
        _add(conn, date(2026, month, 5), "STREAMCO PLUS", -1299, line_item_id=lines["subs"])
        _add(conn, date(2026, month, 9), "Card StreamCo Credit", 1299, line_item_id=lines["subs"])

    items = {s.name: s for s in subs.itemize(conn)}
    credit, plan = items["Card StreamCo Credit"], items["Streamco Plus"]
    assert (credit.is_credit, plan.is_credit) == (True, False)
    assert (credit.cadence_label, credit.typical_cents, credit.monthly_cents) == (
        "Monthly", -1299, -1299,
    )
    assert credit.is_active(TODAY) and credit.next_due(TODAY) == date(2026, 10, 8)
    assert plan.monthly_cents + credit.monthly_cents == 0


def test_candidates_and_moving_them(conn, lines):
    for month in (6, 7, 8):
        _add(conn, date(2026, month, 12), "TUNESTREAM MUSIC", -1099, line_item_id=lines["fun"])
    for month, cents in [(6, -2345), (7, -1980), (8, -3110)]:
        _add(conn, date(2026, month, 1), "FIRST WATCH", cents, line_item_id=lines["fun"])
    for month in (6, 7, 8):
        _add(conn, date(2026, month, 2), "Zelle payment to Pat", -5000)
    _add(conn, date(2026, 8, 20), "TUNESTREAM TRANSFER", -1, excluded=True)

    [music] = subs.candidates(conn)
    assert (music.name, music.line_name, music.typical_cents) == ("Tunestream Music", "Fun", 1099)

    assert tx.recategorize_matching(conn, "TUNESTREAM MUSIC", lines["subs"]) == 3
    assert subs.candidates(conn) == []
    assert [s.name for s in subs.itemize(conn)] == ["Tunestream Music"]
    [rule] = tx.list_rules(conn)
    assert (rule.pattern, rule.line_item_id) == ("TUNESTREAM MUSIC", lines["subs"])


def test_release_moves_charges_out_and_drops_the_rule(conn, lines):
    tx.add_rule(conn, pattern="NINTENDO", line_item_id=lines["subs"])
    _add(conn, date(2026, 8, 8), "NINTENDO CB141", -699)
    _add(conn, date(2026, 6, 2), "NINTENDO CB141", -1999)
    tx.apply_rules(conn)
    _add(conn, date(2026, 8, 3), "NETFLIX.COM", -1549, line_item_id=lines["subs"])
    assert subs.default_other_line(conn).id == lines["fun"]  # no Discretionary line here

    assert subs.release(conn, "nintendo", lines["fun"]) == 2
    assert [s.name for s in subs.itemize(conn)] == ["Netflix.com"]
    assert tx.list_rules(conn) == []
    fixed = planning.list_categories(conn)[1].id
    disc = planning.add_line_item(
        conn, category_id=fixed, name="Discretionary Spending", amount_cents=1
    )
    assert subs.default_other_line(conn).id == disc


def test_split_part_of_a_bill_is_itemized(conn, lines):
    flexible = {c.name: c.id for c in planning.list_categories(conn)}["Flexible Expenses"]
    phone = planning.add_line_item(conn, category_id=flexible, name="Phone", amount_cents=1)
    tx.add_rule(conn, pattern="PHONECO*BILL", line_item_id=phone, split_line_item_id=lines["subs"],
                split_amount_cents=1500, split_note="Cloud storage")
    for month in (7, 8):
        _add(conn, date(2026, month, 26), f"PHONECO*BILL {month}A", -6410)
    tx.apply_rules(conn)

    [plan] = subs.itemize(conn)
    assert (plan.name, plan.typical_cents) == ("Cloud storage", 1500)
    assert plan.cadence_label == "Monthly"
    assert subs.candidates(conn) == []  # the phone remainder: two months is too few to flag

    assert subs.release(conn, "Cloud storage", lines["fun"]) == 2
    assert subs.itemize(conn) == []
    [rule] = tx.list_rules(conn)
    assert rule.split_line_item_id == lines["fun"]  # future bills follow the release


def test_cancelled_subscription_flags_later_charges(conn, lines):
    tx.add_rule(conn, pattern="GYMCO", line_item_id=lines["subs"])
    for month in (6, 7, 8):
        _add(conn, date(2026, month, 21), "GYMCO CLUB 4411", -4500)
    tx.apply_rules(conn)
    [gym] = subs.itemize(conn)
    subs.cancel(conn, gym.pattern, gym.name, date(2026, 9, 10))
    assert [(c.pattern, c.name, c.cancelled_on) for c in subs.list_cancelled(conn)] == [
        ("GYMCO", "Gymco Club", date(2026, 9, 10))
    ]
    assert subs.charges_after_cancel(conn) == []  # earlier charges are fine

    late_id = _add(conn, date(2026, 9, 21), "GYMCO CLUB 4411", -4500)
    _add(conn, date(2026, 9, 25), "GYMCO CLUB REFUND", 4500)  # money back isn't flagged
    [late] = subs.charges_after_cancel(conn)
    assert (late.subscription, late.posted_on, late.amount_cents, late.transaction_id) == (
        "Gymco Club", date(2026, 9, 21), 4500, late_id,
    )
    assert subs.charges_after_cancel(conn, [late_id + 99]) == []  # only the ids asked about

    subs.cancel(conn, "gymco", "Gymco Club", date(2026, 9, 22))  # that was the final charge
    assert subs.charges_after_cancel(conn) == [] and len(subs.list_cancelled(conn)) == 1
    subs.uncancel(conn, "GYMCO")
    assert subs.list_cancelled(conn) == []
    with pytest.raises(ValueError):
        subs.cancel(conn, "  ", "Nothing", date(2026, 9, 1))


def test_no_subscription_line(conn):
    assert subs.subscription_lines(conn) == [] and subs.itemize(conn) == []
