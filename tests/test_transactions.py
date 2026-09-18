from datetime import date

import pytest

from budgetapp import planning
from budgetapp import transactions as tx

SEP = date(2026, 9, 1)


@pytest.fixture
def items(conn):
    cats = {c.name: c.id for c in planning.list_categories(conn)}
    return {
        "pay": planning.add_line_item(
            conn, category_id=cats["Income"], name="Paycheck", amount_cents=500000
        ),
        "groceries": planning.add_line_item(
            conn, category_id=cats["Flexible Expenses"], name="Groceries", amount_cents=60000
        ),
        "coffee": planning.add_line_item(
            conn, category_id=cats["Flexible Expenses"], name="Coffee", amount_cents=3000
        ),
    }


def _add(conn, day, description, cents, **kwargs):
    return tx.add_transaction(
        conn, posted_on=date(2026, 9, day), description=description, amount_cents=cents, **kwargs
    )


def test_budget_vs_actual(conn, items):
    _add(conn, 1, "ACME PAYROLL", 250000, line_item_id=items["pay"])
    _add(conn, 15, "ACME PAYROLL", 250000, line_item_id=items["pay"])
    _add(conn, 3, "WHOLE FOODS", -12000, line_item_id=items["groceries"])
    _add(conn, 9, "WHOLE FOODS REFUND", 2000, line_item_id=items["groceries"])
    _add(conn, 5, "TRANSFER TO SAVINGS", -100000, excluded=True)
    _add(conn, 6, "MYSTERY SHOP", -4500)
    tx.add_transaction(
        conn, posted_on=date(2026, 10, 1), description="NEXT MONTH", amount_cents=-999,
        line_item_id=items["groceries"],
    )

    result = tx.budget_vs_actual(conn, date(2026, 9, 20))
    rows = {r.item.name: r for g in result.groups for r in g.rows}
    assert rows["Paycheck"].actual_cents == 500000
    assert rows["Groceries"].actual_cents == 10000  # refund nets against spending
    assert rows["Groceries"].remaining_cents == 50000
    assert rows["Coffee"].actual_cents == 0
    assert (result.uncategorized_count, result.uncategorized_out_cents) == (1, 4500)
    assert (result.income_actual_cents, result.outflow_actual_cents) == (500000, 10000)
    assert result.month == SEP

    totals = tx.month_totals(conn, SEP)
    assert (totals.inflow_cents, totals.outflow_cents, totals.uncategorized_count) == (
        502000, 16500, 1,
    )


def test_categorize_with_rule_applies_to_others(conn, items):
    first = _add(conn, 2, "STARBUCKS STORE 1234", -550)
    _add(conn, 4, "STARBUCKS STORE 9876", -610)
    _add(conn, 5, "PEETS COFFEE", -400)
    others = tx.categorize(
        conn, first, line_item_id=items["coffee"], remember_pattern="starbucks"
    )
    assert others == 1

    by_desc = {t.description: t for t in tx.list_transactions(conn, SEP)}
    assert by_desc["STARBUCKS STORE 9876"].line_item_name == "Coffee"
    assert by_desc["STARBUCKS STORE 9876"].category_name == "Flexible Expenses"
    assert by_desc["PEETS COFFEE"].line_item_id is None
    [rule] = tx.list_rules(conn)
    assert (rule.pattern, rule.line_item_name) == ("starbucks", "Coffee")

    later = _add(conn, 7, "Starbucks store 555", -300)
    assert tx.apply_rules(conn, [later]) == 1
    assert [t.description for t in tx.list_transactions(conn, SEP, only_uncategorized=True)] == [
        "PEETS COFFEE"
    ]


def test_longest_rule_wins_and_exclusion(conn, items):
    tx.add_rule(conn, pattern="AMAZON", line_item_id=items["groceries"])
    tx.add_rule(conn, pattern="AMAZON PRIME", excluded=True)
    prime = _add(conn, 1, "AMAZON PRIME MEMBERSHIP", -1499)
    market = _add(conn, 2, "AMAZON MKTPLACE", -2000)
    assert tx.apply_rules(conn) == 2
    txns = {t.id: t for t in tx.list_transactions(conn, SEP)}
    assert txns[prime].excluded and txns[prime].line_item_id is None
    assert txns[market].line_item_id == items["groceries"]

    tx.add_rule(conn, pattern="amazon", line_item_id=items["coffee"])  # replaces, case-insensitive
    assert len(tx.list_rules(conn)) == 2
    with pytest.raises(ValueError):
        tx.add_rule(conn, pattern="ab", excluded=True)
    with pytest.raises(ValueError):
        tx.add_rule(conn, pattern="SOMETHING")


def test_import_hash_dedupes_and_validation(conn, items):
    assert _add(conn, 1, "A", -1, import_hash="abc") is not None
    assert _add(conn, 1, "A", -1, import_hash="abc") is None
    with pytest.raises(ValueError):
        _add(conn, 1, "   ", -1)
    with pytest.raises(ValueError):
        _add(conn, 1, "X", -1, line_item_id=9999)


def test_deleting_line_item_uncategorizes(conn, items):
    txn = _add(conn, 3, "WHOLE FOODS", -100, line_item_id=items["groceries"])
    planning.delete_line_item(conn, items["groceries"])
    assert tx.get_transaction(conn, txn).line_item_id is None
    tx.delete_transaction(conn, txn)
    with pytest.raises(LookupError):
        tx.get_transaction(conn, txn)


def test_bank_category_mapping(conn, items):
    whole = _add(conn, 1, "WHOLE FOODS", -1000, bank_category="Groceries")
    kroger = _add(conn, 2, "KROGER", -2000, bank_category="groceries")
    shell = _add(conn, 3, "SHELL", -3000, bank_category="Gas")
    done = _add(conn, 4, "ALREADY", -100, bank_category="Groceries", line_item_id=items["coffee"])
    cats = {c.name.lower(): c for c in tx.bank_categories(conn)}
    assert (cats["groceries"].total, cats["groceries"].uncategorized) == (3, 2)
    assert not cats["gas"].mapped

    mapped = tx.set_bank_category_map(
        conn, {"Groceries": (items["groceries"], False), "Gas": (None, True)}
    )
    assert mapped == 2 and tx.apply_bank_categories(conn) == 3
    txns = {t.id: t for t in tx.list_transactions(conn, SEP)}
    assert txns[whole].line_item_id == txns[kroger].line_item_id == items["groceries"]
    assert txns[shell].excluded
    assert txns[done].line_item_id == items["coffee"]  # already categorized: untouched
    assert txns[whole].bank_category == "Groceries"

    assert tx.set_bank_category_map(conn, {"Gas": None}) == 1
    assert not {c.name: c for c in tx.bank_categories(conn)}["Gas"].mapped


def test_splits_and_split_rules(conn, items):
    fixed = planning.list_categories(conn)[1].id
    phone = planning.add_line_item(conn, category_id=fixed, name="Phone", amount_cents=1)
    bill = _add(conn, 7, "PHONECO*BILL ABC123", -6410, line_item_id=phone)

    split_id = tx.add_split(conn, bill, line_item_id=items["coffee"], amount_cents=1500,
                            note="Cloud plan")
    with pytest.raises(ValueError):  # more than what's left of the charge
        tx.add_split(conn, bill, line_item_id=items["coffee"], amount_cents=6000)
    actual = {r.item.name: r.actual_cents for g in tx.budget_vs_actual(conn, SEP).groups
              for r in g.rows}
    assert (actual["Phone"], actual["Coffee"]) == (4910, 1500)
    [split] = tx.list_splits(conn, bill)
    assert (split.amount_cents, split.line_item_name, split.note) == (-1500, "Coffee", "Cloud plan")
    assert tx.month_totals(conn, SEP).outflow_cents == 6410  # cash totals unchanged
    assert tx.delete_split(conn, split_id) == bill and tx.list_splits(conn, bill) == []

    rule = tx.add_rule(conn, pattern="PHONECO*BILL", line_item_id=phone,
                       split_line_item_id=items["coffee"], split_amount_cents=1500,
                       split_note="Cloud plan")
    assert tx.apply_rule_everywhere(conn, rule) == 1
    assert tx.apply_rule_everywhere(conn, rule) == 1 and len(tx.list_splits(conn, bill)) == 1
    later = _add(conn, 26, "PHONECO*BILL XYZ789", -8025)
    assert tx.apply_rules(conn, [later]) == 1
    assert [s.amount_cents for s in tx.list_splits(conn, later)] == [-1500]
    [saved] = [r for r in tx.list_rules(conn) if r.pattern == "PHONECO*BILL"]
    assert (saved.split_line_item_name, saved.split_amount_cents, saved.split_note) == (
        "Coffee", 1500, "Cloud plan",
    )

    transfer = _add(conn, 8, "TRANSFER", -500, excluded=True)
    with pytest.raises(ValueError):
        tx.add_split(conn, transfer, line_item_id=items["coffee"], amount_cents=100)
    with pytest.raises(ValueError):
        tx.add_rule(conn, pattern="ZZZ", line_item_id=phone, split_amount_cents=100)


def test_suggest_line_item(conn, items):
    fixed = planning.list_categories(conn)[1].id
    card = planning.add_line_item(
        conn, category_id=fixed, name="Credit Card Payment", amount_cents=100
    )
    lines = [i for g in planning.grouped(conn) for i in g.items]
    assert tx.suggest_line_item("Groceries", lines) == items["groceries"]
    assert tx.suggest_line_item("Food & Drink", lines) is None
    assert tx.suggest_line_item("Automotive", lines) is None  # "car" isn't "Card"
    assert tx.suggest_line_item("Bills & Utilities", lines) is None
    assert card  # present but never matched


def test_suggest_line_item_for_chase_and_amex_names(conn):
    fixed = planning.list_categories(conn)[1].id
    names = ["Home Utilities (Power & Gas)", "Fuel", "Pet Care", "Internet",
             "Auto Insurance", "Memberships & Subscriptions",
             "Groceries"]
    ids = {n: planning.add_line_item(conn, category_id=fixed, name=n, amount_cents=100)
           for n in names}
    lines = [i for g in planning.grouped(conn) for i in g.items]

    def suggest(category):
        return tx.suggest_line_item(category, lines)

    assert suggest("Gas") == ids["Fuel"]  # gas stations, not the utility
    assert suggest("Transportation-Fuel") == ids["Fuel"]
    assert suggest("Merchandise & Supplies-Groceries") == ids["Groceries"]
    assert suggest("Communications-Cable & Internet Comm") == ids["Internet"]
    assert suggest("Business Services-Internet Services") == ids["Memberships & Subscriptions"]
    insurance = ids["Auto Insurance"]
    assert suggest("Business Services-Insurance Services") == insurance
    assert suggest("Business Services-Health Care Services") is None  # not "Pet Care"
    assert suggest("Bills & Utilities") == ids["Home Utilities (Power & Gas)"]


@pytest.mark.parametrize(
    ("description", "pattern"),
    [
        ("STARBUCKS STORE 12345 SEATTLE WA", "STARBUCKS STORE"),
        ("SQ *BLUE BOTTLE COFFEE OAKLAND", "SQ *BLUE BOTTLE"),
        ("NETFLIX.COM", "NETFLIX.COM"),
        ("12345", "12345"),
    ],
)
def test_suggest_pattern(description, pattern):
    assert tx.suggest_pattern(description) == pattern
