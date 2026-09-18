from datetime import date

import pytest

from budgetapp import imports, ofx
from budgetapp import networth as nw
from budgetapp import transactions as tx

# OFX 1.x: SGML, values unclosed, aggregates closed, a header block before <OFX>.
SGML = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20261001120000[-5:EST]<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD
<BANKACCTFROM><BANKID>000000000<ACCTID>0000111122223333<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260901<DTEND>20260930
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260901120000[-5:EST]
<TRNAMT>2600.00
<FITID>A-1001
<NAME>PAYROLL ACME
</STMTTRN>
<STMTTRN>
<TRNTYPE>POS
<DTPOSTED>20260902
<TRNAMT>-4.5
<FITID>A-1002
<NAME>CORNER CAFE &amp; BAKERY
<MEMO>CORNER CAFE &amp; BAKERY SPRINGFIELD
</STMTTRN>
<STMTTRN>
<TRNTYPE>POS
<DTPOSTED>20260902
<TRNAMT>-4.50
<FITID>A-1003
<NAME>CORNER CAFE &amp; BAKERY
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1234.56<DTASOF>20260930</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""

# OFX 2.x: XML, a credit card statement, one amount written with a decimal comma.
XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <CREDITCARDMSGSRSV1><CCSTMTTRNRS><TRNUID>1</TRNUID>
    <CCSTMTRS>
      <CURDEF>USD</CURDEF>
      <CCACCTFROM><ACCTID>9999</ACCTID></CCACCTFROM>
      <BANKTRANLIST>
        <STMTTRN>
          <TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261003000000.000</DTPOSTED>
          <TRNAMT>-18,25</TRNAMT><FITID>C-77</FITID>
          <NAME>BOOK NOOK</NAME><MEMO>ONLINE ORDER 4411</MEMO>
        </STMTTRN>
        <STMTTRN>
          <TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20261005</DTPOSTED>
          <TRNAMT>100</TRNAMT><FITID>C-78</FITID><NAME>PAYMENT THANK YOU</NAME>
        </STMTTRN>
      </BANKTRANLIST>
    </CCSTMTRS>
  </CCSTMTTRNRS></CREDITCARDMSGSRSV1>
</OFX>
"""

CSV = b"Date,Description,Amount\n09/01/2026,PAYROLL ACME,2600.00\n"


def _rows(data: bytes) -> list[imports.ImportRow]:
    rows, problems = imports.build_rows(ofx.parse(data), ofx.MAPPING)
    assert problems == []
    return rows


def test_both_ofx_versions_are_recognised_and_csv_is_not():
    assert ofx.looks_like_ofx(SGML) and ofx.looks_like_ofx(XML)
    assert not ofx.looks_like_ofx(CSV)
    assert not ofx.looks_like_ofx(b"<html><body>OFX</body></html>")


def test_sgml_statement():
    rows = _rows(SGML)
    assert [(r.posted_on, r.amount_cents, r.ref) for r in rows] == [
        (date(2026, 9, 1), 260000, "A-1001"),
        (date(2026, 9, 2), -450, "A-1002"),
        (date(2026, 9, 2), -450, "A-1003"),
    ]
    # Entities are decoded; a MEMO that extends the NAME replaces it rather than repeating it.
    assert rows[1].description == "CORNER CAFE & BAKERY SPRINGFIELD"
    assert rows[2].description == "CORNER CAFE & BAKERY"


def test_xml_credit_card_statement():
    parsed = ofx.parse(XML)
    assert parsed.headers == ofx.HEADERS
    assert [row[3] for row in parsed.rows] == ["DEBIT", "CREDIT"]
    rows = _rows(XML)
    assert [(r.posted_on, r.amount_cents) for r in rows] == [
        (date(2026, 10, 3), -1825), (date(2026, 10, 5), 10000)
    ]
    assert rows[0].description == "BOOK NOOK ONLINE ORDER 4411"  # both halves kept


def test_files_this_app_cannot_take_are_refused_plainly():
    two = SGML.replace(b"</STMTRS>", b"</STMTRS><STMTRS><CURDEF>USD<BANKTRANLIST></BANKTRANLIST>")
    with pytest.raises(ValueError, match="holds 2 accounts"):
        ofx.parse(two)
    with pytest.raises(ValueError, match="in EUR"):
        ofx.parse(SGML.replace(b"<CURDEF>USD", b"<CURDEF>EUR"))
    with pytest.raises(ValueError, match="another currency"):  # never echoes odd file text
        ofx.parse(SGML.replace(b"<CURDEF>USD", b"<CURDEF>secret stuff"))
    with pytest.raises(ValueError, match="investment statements"):
        ofx.parse(b"<OFX><INVSTMTMSGSRSV1><INVSTMTRS></INVSTMTRS></INVSTMTMSGSRSV1></OFX>")
    with pytest.raises(ValueError, match="no transactions"):
        ofx.parse(b"<OFX><STMTRS><CURDEF>USD<BANKTRANLIST></BANKTRANLIST></STMTRS></OFX>")
    with pytest.raises(ValueError, match="isn't a readable OFX"):
        ofx.parse(b"OFXHEADER:100\nnothing else")


def test_an_unreadable_transaction_is_reported_not_guessed():
    broken = SGML.replace(
        b"<DTPOSTED>20260902\n<TRNAMT>-4.5", b"<DTPOSTED>yesterday\n<TRNAMT>-4.5", 1
    )
    rows, problems = imports.build_rows(ofx.parse(broken), ofx.MAPPING)
    assert len(rows) == 2 and problems == ["row 2: unrecognized date"]


def test_reimporting_an_overlapping_download_is_exact(conn):
    account = nw.add_account(conn, name="Checking", type="cash")
    first = imports.import_rows(conn, account, _rows(SGML))
    # Two identical coffees on one day stay two: their bank ids differ.
    assert (first.added, first.duplicates) == (3, 0)
    again = imports.import_rows(conn, account, _rows(SGML))
    assert (again.added, again.duplicates) == (0, 3)


def test_switching_an_account_from_csv_to_ofx_does_not_double_it(conn):
    account = nw.add_account(conn, name="Checking", type="cash")
    csv = (b"Date,Description,Amount\n09/01/2026,ACME PAYROLL DEPOSIT,2600.00\n"
           b"09/02/2026,CORNER CAFE,-4.50\n09/02/2026,CORNER CAFE,-4.50\n")
    parsed = imports.parse_csv(csv)
    csv_rows, _ = imports.build_rows(parsed, imports.guess_mapping(parsed.headers))
    assert imports.import_rows(conn, account, csv_rows).added == 3

    # The same three, worded differently by the OFX download: all recognised.
    switched = imports.import_rows(conn, account, _rows(SGML))
    assert (switched.added, switched.duplicates) == (0, 3)
    assert len(tx.list_transactions(conn, date(2026, 9, 1))) == 3

    # And back again: a later CSV of the same days finds them too.
    assert imports.import_rows(conn, account, csv_rows).added == 0


def test_only_the_new_part_of_an_overlap_is_added_across_formats(conn):
    account = nw.add_account(conn, name="Checking", type="cash")
    csv = imports.parse_csv(b"Date,Description,Amount\n09/02/2026,CORNER CAFE,-4.50\n")
    rows, _ = imports.build_rows(csv, imports.guess_mapping(csv.headers))
    imports.import_rows(conn, account, rows)

    # The OFX has that coffee, a second one the same day, and the payroll deposit.
    summary = imports.import_rows(conn, account, _rows(SGML))
    assert (summary.added, summary.duplicates) == (2, 1)
    assert len(tx.list_transactions(conn, date(2026, 9, 1))) == 3
