from dataclasses import replace
from datetime import date

import pytest

from budgetapp import imports, planning
from budgetapp import networth as nw
from budgetapp import transactions as tx
from budgetapp.imports import ImportRow, Mapping

BANK = (
    b"\xef\xbb\xbfDate,Description,Amount,Balance\r\n"
    b'09/01/2026,PAYROLL ACME,"2,600.00",3000.00\r\n'
    b"09/02/2026,STARBUCKS 123,-4.50,2995.50\r\n"
    b"09/02/2026,STARBUCKS 123,-4.50,2991.00\r\n"
)


def test_parse_and_guess_mapping():
    parsed = imports.parse_csv(BANK)
    assert parsed.headers == ["Date", "Description", "Amount", "Balance"]
    assert len(parsed.rows) == 3
    mapping = imports.guess_mapping(parsed.headers)
    assert (mapping.date_col, mapping.description_col, mapping.amount_col) == (0, 1, 2)
    rows, problems = imports.build_rows(parsed, mapping)
    assert problems == []
    assert rows[0] == ImportRow(date(2026, 9, 1), "PAYROLL ACME", 260000)
    assert rows[1].amount_cents == -450


def test_semicolons_debit_credit_and_preamble():
    data = (
        b"Account: ****1234\n"
        b"\n"
        b"Posting Date;Payee;Debit;Credit\n"
        b"2026-09-03;GROCER;45.10;\n"
        b"2026-09-04;REFUND;;5.00\n"
    )
    parsed = imports.parse_csv(data)
    assert parsed.headers == ["Posting Date", "Payee", "Debit", "Credit"]
    mapping = imports.guess_mapping(parsed.headers)
    assert (mapping.amount_col, mapping.debit_col, mapping.credit_col) == (None, 2, 3)
    rows, _ = imports.build_rows(parsed, mapping)
    assert [r.amount_cents for r in rows] == [-4510, 500]


def test_bank_of_america_summary_block_is_skipped():
    # BofA puts a balance summary (label, blank, amount) above the real table and a
    # "Beginning balance" line with no amount as the first data row.
    data = (
        b"Description,,Summary Amt.\n"
        b'Beginning balance as of 03/14/2025,,"812.40"\n'
        b'Total credits,,"1,500.00"\n'
        b'Total debits,,"-2,250.00"\n'
        b'Ending balance as of 05/19/2026,,"62.40"\n'
        b"\n"
        b"Date,Description,Amount,Running Bal.\n"
        b'03/14/2025,Beginning balance as of 03/14/2025,,"812.40"\n'
        b'03/20/2025,"Online Banking transfer to CHK 1234 Conf# 1",-500.00,"312.40"\n'
        b'04/01/2025,"ZELLE FROM JANE",1500.00,"1,812.40"\n'
    )
    parsed = imports.parse_csv(data)
    assert parsed.headers == ["Date", "Description", "Amount", "Running Bal."]
    mapping = imports.guess_mapping(parsed.headers)
    assert (mapping.date_col, mapping.description_col, mapping.amount_col) == (0, 1, 2)
    rows, problems = imports.build_rows(parsed, mapping)
    assert problems == []
    assert [(r.posted_on, r.amount_cents) for r in rows] == [
        (date(2025, 3, 20), -50000), (date(2025, 4, 1), 150000),
    ]
    assert not imports.suggest_flip(rows)


def test_chase_checking_rows_wider_than_header():
    # Chase checking exports end every data row with an extra comma.
    data = (
        b"Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
        b'DEBIT,09/10/2026,"STARBUCKS STORE 123",-5.25,DEBIT_CARD,1234.56,,\n'
        b'CREDIT,09/01/2026,"ACME PAYROLL PPD ID: 123",2000.00,ACH_CREDIT,1239.81,,\n'
    )
    parsed = imports.parse_csv(data)
    assert parsed.headers[:7] == [
        "Details", "Posting Date", "Description", "Amount", "Type", "Balance", "Check or Slip #",
    ]
    mapping = imports.guess_mapping(parsed.headers)
    assert (mapping.date_col, mapping.description_col, mapping.amount_col) == (1, 2, 3)
    rows, problems = imports.build_rows(parsed, mapping)
    assert problems == [] and [r.amount_cents for r in rows] == [-525, 200000]
    assert not imports.suggest_flip(rows)


def test_card_exports_and_flip_suggestion():
    chase_card = imports.parse_csv(
        b"Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
        b"09/09/2026,09/10/2026,NETFLIX.COM,Entertainment,Sale,-15.49,\n"
        b"09/07/2026,09/08/2026,SHELL OIL 123,Gas,Sale,-41.20,\n"
        b"09/05/2026,09/06/2026,Payment Thank You-Mobile,,Payment,500.00,\n"
    )
    mapping = imports.guess_mapping(chase_card.headers)
    assert (mapping.date_col, mapping.description_col, mapping.amount_col) == (0, 2, 5)
    rows, _ = imports.build_rows(chase_card, mapping)
    assert not imports.suggest_flip(rows)  # purchases already negative

    amex = imports.parse_csv(
        b"Date,Description,Amount\n"
        b"09/08/2026,WHOLEFDS MKT 10234,86.42\n"
        b"09/06/2026,DELTA AIR LINES,312.10\n"
        b"09/03/2026,AUTOPAY PAYMENT - THANK YOU,-500.00\n"
    )
    rows, _ = imports.build_rows(amex, imports.guess_mapping(amex.headers))
    assert imports.suggest_flip(rows)  # purchases positive -> flip


CHASE_CARD = (
    b"Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
    b"09/09/2026,09/10/2026,NETFLIX.COM,Entertainment,Sale,-15.49,\n"
    b"09/07/2026,09/08/2026,SHELL OIL 123,Gas,Sale,-41.20,\n"
    b"09/05/2026,09/06/2026,Payment Thank You-Mobile,,Payment,500.00,\n"
)


def test_bank_category_column_and_backfill(conn):
    account = nw.add_account(conn, name="Rewards card", type="credit_card")
    parsed = imports.parse_csv(CHASE_CARD)
    mapping = imports.guess_mapping(parsed.headers)
    assert mapping.category_col == 3
    rows, _ = imports.build_rows(parsed, mapping)
    assert [r.bank_category for r in rows] == ["Entertainment", "Gas", ""]

    # Imported first without the category column; re-importing fills it in, adds nothing.
    plain, _ = imports.build_rows(parsed, replace(mapping, category_col=None))
    assert imports.import_rows(conn, account, plain).added == 3
    again = imports.import_rows(conn, account, rows)
    assert (again.added, again.duplicates, again.categories_filled) == (0, 3, 2)
    assert {c.name: c.total for c in tx.bank_categories(conn)} == {"Entertainment": 1, "Gas": 1}


def test_flip_sign_and_explicit_date_format():
    parsed = imports.parse_csv(b"Trans Date,Description,Amount\n03/09/26,VISA PURCHASE,12.00\n")
    mapping = Mapping(0, 1, amount_col=2, date_format="%d/%m/%y", flip_sign=True)
    rows, _ = imports.build_rows(parsed, mapping)
    assert rows == [ImportRow(date(2026, 9, 3), "VISA PURCHASE", -1200)]


def test_bad_rows_reported_without_echoing_cells():
    parsed = imports.parse_csv(
        b"Date,Description,Amount\n"
        b"secret-ish text,X,1.00\n"
        b"09/01/2026,OK,abc\n"
        b"09/02/2026,GOOD,(1.00)\n"
        b"09/03/2026,ZERO AUTH,0.00\n"
    )
    rows, problems = imports.build_rows(parsed, imports.guess_mapping(parsed.headers))
    assert rows == [ImportRow(date(2026, 9, 2), "GOOD", -100)]
    assert len(problems) == 2
    assert not any("secret-ish" in p or "abc" in p for p in problems)


def test_headerless_file_and_iso_timestamps():
    parsed = imports.parse_csv(b"2026-09-01T10:15:00,COFFEE,-4.50\n2026-09-02,LUNCH,-12.00\n")
    assert parsed.headers == ["Column 1", "Column 2", "Column 3"]
    rows, problems = imports.build_rows(parsed, Mapping(0, 1, amount_col=2))
    assert problems == [] and rows[0].posted_on == date(2026, 9, 1)


def test_rejects_empty_and_non_csv():
    for data in (b"", b"   ", b"just one column\nanother line\n"):
        with pytest.raises(ValueError):
            imports.parse_csv(data)
    parsed = imports.parse_csv(BANK)
    with pytest.raises(ValueError):
        imports.build_rows(parsed, Mapping(0, 9, amount_col=2))  # column out of range
    with pytest.raises(ValueError):
        imports.build_rows(parsed, Mapping(0, 1))  # no amount column


def test_import_dedupes_but_keeps_identical_rows_in_one_file(conn):
    account = nw.add_account(conn, name="Checking", type="cash")
    parsed = imports.parse_csv(BANK)
    rows, _ = imports.build_rows(parsed, imports.guess_mapping(parsed.headers))

    first = imports.import_rows(conn, account, rows)
    assert (first.added, first.duplicates, first.latest) == (3, 0, date(2026, 9, 2))
    again = imports.import_rows(conn, account, rows)
    assert (again.added, again.duplicates) == (0, 3)
    assert len(tx.list_transactions(conn, date(2026, 9, 1))) == 3


def test_import_applies_rules_and_remembers_mapping(conn):
    account = nw.add_account(conn, name="Checking", type="cash")
    cats = {c.name: c.id for c in planning.list_categories(conn)}
    coffee = planning.add_line_item(
        conn, category_id=cats["Flexible Expenses"], name="Coffee", amount_cents=3000
    )
    tx.add_rule(conn, pattern="STARBUCKS", line_item_id=coffee)
    parsed = imports.parse_csv(BANK)
    mapping = imports.guess_mapping(parsed.headers)
    rows, _ = imports.build_rows(parsed, mapping)
    assert imports.import_rows(conn, account, rows).categorized == 2

    assert imports.get_profile(conn, account) is None
    imports.save_profile(conn, account, mapping)
    assert imports.get_profile(conn, account) == mapping


# ---------------------------------------------------------------- dates, one order per file
def test_one_day_over_12_makes_the_whole_file_day_first():
    parsed = imports.parse_csv(
        b"Date,Description,Amount\n03/04/2026,RENT,-900.00\n13/04/2026,GROCER,-45.10\n"
    )
    order = imports.date_order([row[0] for row in parsed.rows])
    assert order.day_first and not order.ambiguous
    rows, problems = imports.build_rows(parsed, imports.guess_mapping(parsed.headers))
    assert problems == []
    assert [r.posted_on for r in rows] == [date(2026, 4, 3), date(2026, 4, 13)]  # 3 April


def test_month_first_stays_the_default_and_says_when_it_is_a_guess():
    ambiguous = imports.date_order(["03/04/2026", "11/12/2026"])
    assert (ambiguous.day_first, ambiguous.ambiguous) == (False, True)
    proven = imports.date_order(["03/04/2026", "04/30/2026"])
    assert (proven.day_first, proven.ambiguous) == (False, False)
    assert imports.date_order(["2026-04-03", "Apr 3, 2026"]) == imports.DateOrder(False, False)
    with pytest.raises(ValueError, match="day first and some the month"):
        imports.date_order(["13/04/2026", "04/30/2026"])


def test_dotted_european_dates():
    parsed = imports.parse_csv(b"Date;Description;Amount\n31.12.2026;SHOP;-9.99\n")
    rows, _ = imports.build_rows(parsed, imports.guess_mapping(parsed.headers))
    assert rows[0].posted_on == date(2026, 12, 31)
    explicit, _ = imports.build_rows(
        parsed, replace(imports.guess_mapping(parsed.headers), date_format="%d.%m.%Y")
    )
    assert explicit[0].posted_on == date(2026, 12, 31)


def test_a_preview_sample_uses_the_whole_files_date_order():
    parsed = imports.parse_csv(
        b"Date,Description,Amount\n03/04/2026,A,-1.00\n05/04/2026,B,-1.00\n13/04/2026,C,-1.00\n"
    )
    sample = imports.ParsedCsv(parsed.headers, parsed.rows[:1])  # 03/04 alone reads as March
    mapping = imports.guess_mapping(parsed.headers)
    whole = imports.date_order([row[0] for row in parsed.rows])
    rows, _ = imports.build_rows(sample, mapping, day_first=whole.day_first)
    assert rows[0].posted_on == date(2026, 4, 3)


# ---------------------------------------------------------------- CR / DR amounts
def test_credit_and_debit_markers():
    assert imports.parse_amount("12.30 CR") == 1230
    assert imports.parse_amount("12.30CR") == 1230
    assert imports.parse_amount("12.30 DR") == -1230
    assert imports.parse_amount("$1,012.30 dr") == -101230
    assert imports.parse_amount("12.30-") == -1230
    assert imports.parse_amount("-12.30 CR") == 1230  # the marker wins over a stray sign
    assert imports.parse_amount("(12.30)") == -1230
    with pytest.raises(ValueError):
        imports.parse_amount("CR")

    parsed = imports.parse_csv(
        b"Date,Details,Amount\n09/01/2026,REFUND,5.00 CR\n09/02/2026,SHOP,20.00 DR\n"
    )
    rows, problems = imports.build_rows(parsed, imports.guess_mapping(parsed.headers))
    assert problems == [] and [r.amount_cents for r in rows] == [500, -2000]


# ---------------------------------------------------------------- columns judged by content
def test_a_headerless_export_is_mapped_by_what_its_columns_hold():
    # date, amount, a marker column, an empty check-number column, then the description.
    data = (b'"09/01/2026","-12.40","*","","CORNER STORE 12 SPRINGFIELD"\n'
            b'"09/02/2026","2600.00","*","","PAYROLL ACME"\n'
            b'"09/03/2026","-60.00","*","","FUEL STOP 9"\n')
    parsed = imports.parse_csv(data)
    assert parsed.headers[0] == "Column 1"  # no header row
    mapping = imports.guess_mapping(parsed.headers, parsed.rows)
    assert (mapping.date_col, mapping.amount_col, mapping.description_col) == (0, 1, 4)
    rows, problems = imports.build_rows(parsed, mapping)
    assert problems == [] and rows[0].description == "CORNER STORE 12 SPRINGFIELD"

    # Headers alone, as before, fall back to column positions.
    blind = imports.guess_mapping(parsed.headers)
    assert (blind.date_col, blind.description_col) == (0, 1)


def test_the_amount_beats_a_running_balance_and_a_reference_number():
    data = (b"Posted,Ref,Detail,Value,Running\n"
            b"09/01/2026,40001,PAYROLL ACME,2600.00,3000.00\n"
            b"09/02/2026,40002,CORNER CAFE,-4.50,2995.50\n"
            b"09/03/2026,40003,BOOK NOOK,-18.25,2977.25\n")
    parsed = imports.parse_csv(data)
    mapping = imports.guess_mapping(parsed.headers, parsed.rows)
    # None of "Posted", "Detail" or "Value" is in the header word lists.
    assert (mapping.date_col, mapping.description_col, mapping.amount_col) == (0, 2, 3)


def test_header_words_still_win_over_content():
    parsed = imports.parse_csv(BANK)
    assert imports.guess_mapping(parsed.headers, parsed.rows) == imports.guess_mapping(
        parsed.headers
    )


def test_a_transaction_id_column_makes_csv_reimports_exact(conn):
    account = nw.add_account(conn, name="Checking", type="cash")
    parsed = imports.parse_csv(
        b"Date,Description,Amount,Transaction ID\n"
        b"09/02/2026,CORNER CAFE,-4.50,T1\n09/02/2026,CORNER CAFE,-4.50,T2\n"
    )
    mapping = replace(imports.guess_mapping(parsed.headers), id_col=3)
    rows, _ = imports.build_rows(parsed, mapping)
    assert [r.ref for r in rows] == ["T1", "T2"]
    assert imports.import_rows(conn, account, rows).added == 2
    assert imports.import_rows(conn, account, rows).duplicates == 2
    assert Mapping.from_json(mapping.to_json()) == mapping
    old_profile = '{"date_col": 0, "description_col": 1, "amount_col": 2}'
    assert Mapping.from_json(old_profile).id_col is None  # saved before ids existed
