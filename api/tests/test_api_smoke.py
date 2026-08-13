"""Database-backed smoke tests for the new/changed endpoints and services.

Skipped automatically when no PostgreSQL is reachable - see conftest.py.
All figures below are fictional test fixtures, not real financial data.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .conftest import requires_db


@requires_db
def test_refresh_current_prices_does_not_crash_for_watchlist_only_ticker(db_session, monkeypatch):
    """Regression test for the bug where `rate` was undefined when a ticker had
    no PortfolioPosition (only StockTransaction/WatchlistStock rows)."""
    from app import stock_services
    from app.models import StockTransaction, WatchlistStock

    db_session.add(
        StockTransaction(
            traded_on=date(2024, 2, 1),
            movement_type="Nákup",
            instrument_name="Apple",
            ticker="AAPL",
            quantity=Decimal("2"),
            unit_price_ccy=Decimal("150"),
            gross_amount_ccy=Decimal("300"),
            currency="USD",
            amount_czk=Decimal("6900"),
        )
    )
    db_session.add(
        WatchlistStock(watched_on=date(2024, 1, 1), name="Apple", ticker="AAPL", limit_price=Decimal("200"), currency="USD")
    )
    db_session.commit()

    monkeypatch.setattr(stock_services, "fetch_yahoo_price", lambda ticker: {"price": Decimal("190"), "currency": "USD"})

    result = stock_services.refresh_current_prices(db_session, threshold_pct=Decimal("5"))
    assert result["updated"] == 1
    assert result["errors"] == []


@requires_db
def test_compute_alerts_reports_watchlist_breach_and_drawdown(db_session):
    from app import stock_services
    from app.models import PortfolioPosition, WatchlistStock

    db_session.add(
        WatchlistStock(
            watched_on=date(2024, 1, 1),
            name="Apple",
            ticker="AAPL",
            limit_price=Decimal("200"),
            current_price=Decimal("180"),
            currency="USD",
        )
    )
    db_session.add(
        PortfolioPosition(
            ticker="MSFT",
            name="Microsoft",
            quantity=Decimal("1"),
            current_price=Decimal("100"),
            currency="USD",
            market_value_czk=Decimal("2000"),
            invested_czk=Decimal("3000"),
            profit_czk=Decimal("-1000"),
            profit_pct=Decimal("-0.3333"),
        )
    )
    db_session.commit()

    alerts = stock_services.compute_alerts(db_session, threshold_pct=Decimal("10"))
    assert any(a["ticker"] == "AAPL" for a in alerts["watchlist_limit_breaches"])
    assert any(a["ticker"] == "MSFT" for a in alerts["portfolio_drawdowns"])


@requires_db
def test_build_ticker_history_accumulates_purchases(db_session, monkeypatch):
    from app import stock_services
    from app.models import StockTransaction

    db_session.add(
        StockTransaction(
            traded_on=date(2024, 2, 1),
            movement_type="Nákup",
            instrument_name="Apple",
            ticker="AAPL",
            quantity=Decimal("2"),
            unit_price_ccy=Decimal("150"),
            gross_amount_ccy=Decimal("300"),
            currency="USD",
            amount_czk=Decimal("6900"),
        )
    )
    db_session.commit()

    def fake_history(ticker, date_from, date_to):
        points = []
        current = date_from
        price = Decimal("140")
        while current <= date_to:
            points.append((current, price))
            price += Decimal("1")
            current += timedelta(days=1)
        return {"currency": "USD", "points": points}

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", fake_history)

    history = stock_services.build_ticker_history(db_session, "AAPL", date(2024, 2, 1), date(2024, 2, 5))
    assert len(history["rows"]) == 5
    assert history["rows"][0]["buy_quantity"] == 2.0
    assert history["summary"]["cumulative_quantity"] == 2.0


@requires_db
def test_asset_endpoints_expose_computed_interest_plan(client):
    from app.models import Asset
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        asset = Asset(
            code="TEST-01",
            name="Testovaci byt",
            asset_type="Byt",
            total_value=Decimal("5000000"),
            own_funds=Decimal("1000000"),
            borrowed_amount=Decimal("4000000"),
            interest_rate=Decimal("0.0489"),
            loan_years=Decimal("15"),
            borrowed_from=date(2024, 7, 1),
        )
        session.add(asset)
        session.commit()
        asset_id = str(asset.id)
    finally:
        session.close()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    assets_response = client.get("/assets", headers=headers)
    assert assets_response.status_code == 200
    assert "computed_interest_plan" in assets_response.json()[0]

    projection_response = client.get(f"/assets/{asset_id}/interest-projection", headers=headers)
    assert projection_response.status_code == 200
    payload = projection_response.json()
    assert payload["total_computed"] > 0
    assert "2026" in payload["computed_plan"]


@requires_db
def test_stocks_alerts_endpoint_requires_auth(client):
    unauthenticated = client.get("/stocks/alerts")
    assert unauthenticated.status_code == 401

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    response = client.get("/stocks/alerts", headers=headers)
    assert response.status_code == 200
    assert "watchlist_limit_breaches" in response.json()


@requires_db
def test_recalculate_stocks_date_from_preserves_earlier_rows(db_session, monkeypatch):
    """`date_from` should behave like the VBA "Zpracovat od" cell: cumulative
    totals are always computed from the very first transaction (so later rows
    stay correct), but rows before `date_from` are left untouched in the DB
    rather than being deleted and recomputed.

    Note: `recalculate_stocks` fills in every calendar day up to today (not
    just days with a purchase), so exact row counts depend on the wall clock -
    this test avoids hard-coding a day count and instead checks the invariant
    that a full recompute and an incremental one touch the same total number
    of rows, plus the actual before/after values of specific dates.
    """
    from sqlalchemy import func, select

    from app import stock_services
    from app.models import DailyStatistic, StockTransaction

    # recalculate_stocks now fetches historical prices per ticker to compute
    # real market value - stub it out so this test stays a pure DB test with
    # no real network dependency (it only asserts on the invested-amount
    # columns, not market value).
    monkeypatch.setattr(stock_services, "fetch_yahoo_history", lambda ticker, date_from, date_to: {"currency": "CZK", "points": []})

    for day, qty, price in [(date(2024, 1, 5), 1, 100), (date(2024, 1, 20), 1, 110), (date(2024, 2, 10), 1, 120)]:
        db_session.add(
            StockTransaction(
                traded_on=day,
                movement_type="Nákup",
                instrument_name="Apple",
                ticker="AAPL",
                quantity=Decimal(qty),
                unit_price_ccy=Decimal(price),
                gross_amount_ccy=Decimal(price),
                currency="CZK",
                amount_czk=Decimal(price),
            )
        )
    db_session.commit()

    # Full recompute establishes the baseline history.
    full = stock_services.recalculate_stocks(db_session, dry_run=False)
    assert full["date_from"] is None
    total_rows_after_full = db_session.scalar(select(func.count()).select_from(DailyStatistic))
    assert total_rows_after_full == full["daily_statistics"]

    jan5_before = db_session.get(DailyStatistic, date(2024, 1, 5))
    assert jan5_before is not None

    # Simulate a manually edited / imported historical row, then confirm an
    # incremental recalculate from a later date leaves it alone.
    jan5_before.total_czk = Decimal("999999")
    db_session.commit()

    date_from = date(2024, 2, 1)
    incremental = stock_services.recalculate_stocks(db_session, dry_run=False, date_from=date_from)
    assert incremental["date_from"] == date_from

    # Incremental recalculate only rewrites dates that already existed - total row count is unchanged.
    total_rows_after_incremental = db_session.scalar(select(func.count()).select_from(DailyStatistic))
    assert total_rows_after_incremental == total_rows_after_full

    jan5_after = db_session.get(DailyStatistic, date(2024, 1, 5))
    assert jan5_after.total_czk == Decimal("999999")  # untouched, before date_from

    feb10_after = db_session.get(DailyStatistic, date(2024, 2, 10))
    assert feb10_after is not None
    # Cumulative total still reflects all three purchases (100+110+120), not just the Feb one.
    assert feb10_after.total_czk == Decimal("330")


@requires_db
def test_recalculate_stocks_ignores_watchlist_tip_and_plan_rows(db_session, monkeypatch):
    """Regression test: watchlist/tip/plan rows in `stock_transactions` (imported
    verbatim from the "Akcie" sheet, which mixes real purchases with hypothetical
    ones) must not be counted as real purchases - AkcieStatistika.bas only ever
    processes "nakup"/"prodej" rows. Before the fix, `movement_is_buy` treated
    "tip"/"watchlist"/"plán" as regular buys, which inflated portfolio quantity/
    invested_czk and the daily statistics totals beyond what the imported Excel
    figures showed."""
    from app import stock_services
    from app.models import PortfolioPosition, StockTransaction

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", lambda ticker, date_from, date_to: {"currency": "CZK", "points": []})

    # A real purchase of 1 share for 1000 CZK.
    db_session.add(
        StockTransaction(
            traded_on=date(2024, 1, 10),
            movement_type="Nákup",
            instrument_name="Apple",
            ticker="AAPL",
            quantity=Decimal("1"),
            unit_price_ccy=Decimal("1000"),
            gross_amount_ccy=Decimal("1000"),
            currency="CZK",
            amount_czk=Decimal("1000"),
        )
    )
    # A watchlist/tip/plan row for the same ticker that must be ignored entirely.
    for movement in ("Tip", "Sledované", "Plán"):
        db_session.add(
            StockTransaction(
                traded_on=date(2023, 1, 1),  # earlier than the real purchase, to also exercise first_buy_date
                movement_type=movement,
                instrument_name="Apple",
                ticker="AAPL",
                quantity=Decimal("100"),
                unit_price_ccy=Decimal("1"),
                gross_amount_ccy=Decimal("100"),
                currency="CZK",
                amount_czk=Decimal("100"),
            )
        )
    db_session.commit()

    stock_services.recalculate_stocks(db_session, dry_run=False)

    position = db_session.get(PortfolioPosition, "AAPL")
    assert position is not None
    assert position.quantity == Decimal("1")
    assert position.invested_czk == Decimal("1000")
    # The real purchase date, not the earlier watchlist/tip/plan row's date.
    assert position.first_buy_date == date(2024, 1, 10)


@requires_db
def test_recalculate_stocks_unrealized_profit_reflects_price_history(db_session, monkeypatch):
    """Regression test for the actual bug this feature was supposed to fix:
    'Nereal. zisk' (unrealized profit) and the 'Hod./Celk. Hod.' market-value
    columns must reflect the ticker's real historical price on each day
    (AkcieStatistika.bas's GetPriceAtOrBefore), not just the invested amount
    re-expressed via that day's FX rate. Before the fix, a CZK-denominated
    purchase always showed ~0 unrealized profit no matter how much the share
    price actually moved, because "value" was computed purely from money
    invested, never from quantity held x historical price."""
    from app import stock_services
    from app.models import DailyStatistic, StockTransaction

    buy_date = date(2024, 1, 5)
    later_date = date(2024, 1, 10)

    db_session.add(
        StockTransaction(
            traded_on=buy_date,
            movement_type="Nákup",
            instrument_name="Test Corp",
            ticker="TEST",
            quantity=Decimal("10"),
            unit_price_ccy=Decimal("100"),
            gross_amount_ccy=Decimal("1000"),
            currency="CZK",
            amount_czk=Decimal("1000"),
        )
    )
    db_session.commit()

    def fake_history(ticker, date_from, date_to):
        assert ticker == "TEST"
        # Price rose from 100 to 150 CZK/share between the purchase and later_date.
        return {"currency": "CZK", "points": [(buy_date, Decimal("100")), (later_date, Decimal("150"))]}

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", fake_history)

    stock_services.recalculate_stocks(db_session, dry_run=False)

    buy_day_row = db_session.get(DailyStatistic, buy_date)
    assert buy_day_row is not None
    assert buy_day_row.total_value_czk == Decimal("1000")  # 10 x 100, bought at cost
    assert buy_day_row.unrealized_profit_czk == Decimal("0")

    later_row = db_session.get(DailyStatistic, later_date)
    assert later_row is not None
    assert later_row.value_czk == Decimal("1500")  # 10 shares x 150 CZK
    assert later_row.total_value_czk == Decimal("1500")
    assert later_row.invested_czk == Decimal("1000")  # unchanged - still only ever paid 1000
    assert later_row.unrealized_profit_czk == Decimal("500")  # 1500 - 1000, the real price gain
