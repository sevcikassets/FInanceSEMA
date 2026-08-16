"""Unit tests for the pure (non-database) helpers in app.stock_services."""

from datetime import date
from decimal import Decimal

from app.stock_services import (
    _extract_quote,
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


def test_parse_patria_text_handles_double_tab_spacer_columns():
    # Patria's own web export copy-pastes each column as value+empty-spacer
    # (two tabs between every field, including a trailing pair) - this used
    # to silently misalign every field one column early/late, e.g. an ISIN
    # landing in the "currency" slot, which then crashed the DB insert.
    text = (
        "Datum obchodu\t\tPočet kusů\t\tSměr\t\tNázev cenného papíru\t\tProvize\t\tTyp pokynu\t\tTrh\t\tProtistrana\n"
        "Datum vypořádání\t\tCena za kus\t\tAUV\t\tISIN\t\tPoplatek trhu\t\tCelková cena\t\tMěna\t\t\n"
        "\t\t\t\t\t\t\t\t\t\t\t\t\t\t\n"
        "12.08.2026 16:33:19\t\t1,00\t\tNákup\t\tSPDR USA S/C VALUE\t\t5,15\t\tLimit\t\tXETR\t\t\n"
        "14.08.2026\t\t82,40\t\t0,00\t\tIE00BSPLC413\t\t0,00\t\t87,55\t\tEUR\t\t\n"
    )
    trades = parse_patria_text(text)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.traded_on == date(2026, 8, 12)
    assert trade.quantity == Decimal("1")
    assert trade.movement_type == "Nákup"
    assert trade.instrument_name == "SPDR USA S/C VALUE"
    assert trade.market == "XETR"
    assert trade.unit_price_ccy == Decimal("82.40")
    assert trade.isin == "IE00BSPLC413"
    assert trade.currency == "EUR"
    assert trade.gross_amount_ccy == Decimal("87.55")


def test_extract_quote_reads_chart_endpoint_shape():
    # /v8/finance/chart response shape
    payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 123.45, "currency": "USD"}}]}}
    assert _extract_quote(payload) == (123.45, "USD")


def test_extract_quote_reads_v7_quote_endpoint_shape():
    # /v7/finance/quote response shape - AktualizujHodnotu.bas's GetStooqPrice
    # falls back to this when the chart endpoint is throttled/unavailable.
    payload = {"quoteResponse": {"result": [{"regularMarketPrice": 67.89, "currency": "EUR"}]}}
    assert _extract_quote(payload) == (67.89, "EUR")


def test_extract_quote_returns_none_when_both_shapes_empty():
    assert _extract_quote({"chart": {"result": []}, "quoteResponse": {"result": []}}) == (None, None)
