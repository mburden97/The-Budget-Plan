"""OFX / QFX statements: the "Quicken" / "Money" / "Web Connect" download most US banks offer.

OFX 1.x is SGML: a value follows its tag with no closing tag, and only aggregates such as
<STMTTRN> are closed. OFX 2.x is XML. Both are read here by plain pattern matching over the
text -- never an XML parser -- so there are no entities to expand and nothing outside the
file is ever fetched. Bank files often bend the spec, and this reader is forgiving about it.

Only bank and credit-card statements are read; an investment statement has no transactions
in this app's sense. The result is a ParsedCsv with fixed columns, so an OFX file goes
through the same preview, import and de-duplication as a CSV. Each transaction keeps the
bank's own id (FITID), which makes re-importing an overlapping download exact.
"""

from __future__ import annotations

import html
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from budgetapp.imports import MAX_ROWS, Mapping, ParsedCsv

HEADERS = ["Date", "Description", "Amount", "Type", "Transaction ID"]
MAPPING = Mapping(date_col=0, description_col=1, amount_col=2, date_format="%Y-%m-%d", id_col=4)

_STATEMENT = re.compile(r"<(STMTRS|CCSTMTRS)>", re.I)
_TRANSACTION = re.compile(r"<STMTTRN>", re.I)
_END_OF_TRANSACTION = re.compile(r"</STMTTRN>|</BANKTRANLIST>", re.I)
_CURRENCY = re.compile(r"[A-Z]{3}")


def looks_like_ofx(data: bytes) -> bool:
    """True for an OFX 1 (SGML) or OFX 2 (XML) file, whatever it is named."""
    head = data[:4096].lstrip().upper()
    return head.startswith(b"OFXHEADER") or b"<?OFX" in head or b"<OFX>" in head


def parse(data: bytes) -> ParsedCsv:
    """The transactions of the one bank or card statement in an OFX file.

    Errors never quote the file: messages can end up in the session cookie.
    """
    text = _decode(data)
    start = text.upper().find("<OFX>")
    if start < 0:
        raise ValueError("This isn't a readable OFX file.")
    statements = _STATEMENT.split(text[start:])[2::2]  # split keeps the tag: [pre, tag, body, ...]
    if not statements:
        raise ValueError(
            "This OFX file has no bank or card transactions "
            "(investment statements aren't supported)."
        )
    if len(statements) > 1:
        raise ValueError(
            f"This file holds {len(statements)} accounts. "
            "Download one account at a time from your bank."
        )
    body = statements[0]
    currency = (_value(body, "CURDEF") or "USD").upper()
    if currency != "USD":
        shown = currency if _CURRENCY.fullmatch(currency) else "another currency"
        raise ValueError(f"This statement is in {shown}; the app keeps everything in US dollars.")

    rows: list[list[str]] = []
    for piece in _TRANSACTION.split(body)[1:]:
        block = _END_OF_TRANSACTION.split(piece, maxsplit=1)[0]
        rows.append([
            _date(_value(block, "DTPOSTED") or _value(block, "DTUSER")),
            _description(_value(block, "NAME"), _value(block, "MEMO")),
            _amount(_value(block, "TRNAMT")),
            _value(block, "TRNTYPE").upper(),
            _value(block, "FITID"),
        ])
        if len(rows) > MAX_ROWS:
            raise ValueError(f"Too many transactions; import at most {MAX_ROWS:,} at a time.")
    if not rows:
        raise ValueError("This OFX statement has no transactions.")
    return ParsedCsv(list(HEADERS), rows)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")  # OFX 1 headers usually say CHARSET:1252


def _value(block: str, tag: str) -> str:
    """A leaf element's text. In SGML it runs to the next tag or line end; in XML to </TAG>."""
    match = re.search(rf"<{tag}>([^<\r\n]*)", block, re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def _date(raw: str) -> str:
    """OFX dates are YYYYMMDD, optionally followed by a time and a zone: keep the day."""
    digits = raw[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return raw  # left as it is, so the preview reports the row as unreadable


def _amount(raw: str) -> str:
    """Signed, in dollars, with exactly two decimals. Some banks write a decimal comma."""
    text = raw.replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return raw
    if not value.is_finite():
        return raw
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _description(name: str, memo: str) -> str:
    """NAME is often cut at 32 characters and MEMO carries the rest: use both, once."""
    name, memo = " ".join(name.split()), " ".join(memo.split())
    if not memo or memo in name:
        return name
    if not name or name in memo:
        return memo
    return f"{name} {memo}"
