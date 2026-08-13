"""Unit tests for the pure (non-database) helpers in app.stock_services."""

from datetime import date
from decimal import Decimal

from app.stock_services import (
    decimal_or_zero,
    movement_is_buy,
    movement_is_dividend,
    movement_is_sell,
    normalize_ticker,
    parse_date,
    parse_patria_text,
    to_number,
)


def test_decimal_or_zero_handles_czech_number_format():
    assert decimal_or_zero("1 234,56") == Decimal("1234.56")
    assert decimal_or_zero(None) == Decimal("0")
    assert decimal_or_zero("") == Decimal("0")
    assert decimal_or_zero("not-a-number") == Decimal("0")


def test_parse_date_accepts_known_formats():
    assert parse_date("01.02.2024") == date(2024, 2, 1)
    assert parse_date("2024-02-01") == date(2024, 2, 1)
    assert parse_date("01.02.2024 10:30:00") == date(2024, 2, 1)
    assert parse_date("not a date") is None


def test_normalize_ticker_adds_exchange_suffix():
    assert normalize_ticker("DBK", "XETR") == "DBK.DE"
    assert normalize_ticker("AAPL", "XNAS") == "AAPL"
    assert normalize_ticker("dbk.de") == "DBK.DE"
    assert normalize_ticker(None) is None


def test_movement_classification_helpers():
    assert movement_is_buy("Nákup")
    assert movement_is_buy("nakup")
    assert not movement_is_buy("Prodej")
    # AkcieStatistika.bas is explicit: "POUZE nakup a prodej -- vsechny ostatni
    # pohyby (Tip apod.) se preskakuji". Watchlist/tip/plan rows are hypothetical
    # and must never be counted as real purchases.
    assert not movement_is_buy("watchlist")
    assert not movement_is_buy("tip")
    assert not movement_is_buy("plán")
    assert not movement_is_buy("plan")
    assert movement_is_sell("prodej")
    assert movement_is_dividend("Dividenda")
    assert not movement_is_dividend("Nákup")


def test_to_number_converts_decimal_to_float_and_passes_through_other_types():
    assert to_number(Decimal("12.50")) == 12.5
    assert isinstance(to_number(Decimal("1")), float)
    assert to_number(None) is None
    assert to_number("text") == "text"


def test_parse_patria_text_builds_buy_and_sell_trades():
    # Two-line-per-trade format copy-pasted from Patria: first line = date/qty/direction/
    # name/fee/exchange, second line = time/unit-price/.../isin/fee/total/currency.
    text = (
        "01.02.2024\t10\tNákup\tApple Inc\t1,5\t0\tXNAS\n"
        "10:00:00\t150,00\t0\tUS0378331005\t0\t1500,00\tUSD\n"
        "05.02.2024\t4\tProdej\tApple Inc\t1,5\t0\tXNAS\n"
        "10:05:00\t160,00\t0\tUS0378331005\t0\t640,00\tUSD\n"
    )
    trades = parse_patria_text(text)
    assert len(trades) == 2

    buy, sell = trades
    assert buy.movement_type == "Nákup"
    assert buy.quantity == Decimal("10")
    assert buy.isin == "US0378331005"
    assert buy.currency == "USD"

    assert sell.movement_type == "Prodej"
    assert sell.quantity == Decimal("-4")


def test_parse_patria_text_ignores_incomplete_trailing_lines():
    text = "01.02.2024\t10\tNákup\tApple Inc\t1,5\t0\tXNAS\n"
    assert parse_patria_text(text) == []
