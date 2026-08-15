"""Database-backed smoke tests for the new/changed endpoints and services.

Skipped automatically when no PostgreSQL is reachable - see conftest.py.
All figures below are fictional test fixtures, not real financial data.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from .conftest import requires_db


@requires_db
def test_refresh_current_prices_does_not_crash_for_watchlist_only_ticker(db_session, portfolio_id, monkeypatch):
    """Regression test for the bug where `rate` was undefined when a ticker had
    no PortfolioPosition (only StockTransaction/WatchlistStock rows)."""
    from app import stock_services
    from app.models import StockTransaction, WatchlistStock

    db_session.add(
        StockTransaction(
            portfolio_id=portfolio_id,
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
        WatchlistStock(
            portfolio_id=portfolio_id,
            watched_on=date(2024, 1, 1),
            name="Apple",
            ticker="AAPL",
            limit_price=Decimal("200"),
            currency="USD",
        )
    )
    db_session.commit()

    monkeypatch.setattr(stock_services, "fetch_yahoo_price", lambda ticker: {"price": Decimal("190"), "currency": "USD"})

    result = stock_services.refresh_current_prices(db_session, portfolio_id, threshold_pct=Decimal("5"))
    assert result["updated"] == 1
    assert result["errors"] == []


@requires_db
def test_compute_alerts_reports_watchlist_breach_and_drawdown(db_session, portfolio_id):
    from app import stock_services
    from app.models import DailyStatistic, PortfolioPosition, WatchlistStock

    db_session.add(
        WatchlistStock(
            portfolio_id=portfolio_id,
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
            portfolio_id=portfolio_id,
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
    # The daily-statistics "Upozorneni" text is where day-over-day price
    # movers actually get computed (from real historical closes, during a
    # recalculation) - compute_alerts must surface them as a third,
    # structured category instead of leaving them buried in that text blob.
    db_session.add(
        DailyStatistic(
            portfolio_id=portfolio_id,
            stat_date=date(2024, 1, 10),
            alerts="INTC (D:-11.3%), AMD (D:+12.0%) | Chybí cena: XYZ",
        )
    )
    db_session.commit()

    alerts = stock_services.compute_alerts(db_session, portfolio_id, threshold_pct=Decimal("10"))
    assert any(a["ticker"] == "AAPL" for a in alerts["watchlist_limit_breaches"])
    assert any(a["ticker"] == "MSFT" for a in alerts["portfolio_drawdowns"])
    movers = {m["ticker"]: m["change_pct"] for m in alerts["daily_movers"]}
    assert movers == {"INTC": -11.3, "AMD": 12.0}
    assert "XYZ" not in movers  # the missing-price part must not be parsed as a mover
    assert alerts["daily_movers_as_of"] == "2024-01-10"


@requires_db
def test_build_ticker_history_accumulates_purchases(db_session, portfolio_id, monkeypatch):
    from app import stock_services
    from app.models import StockTransaction

    db_session.add(
        StockTransaction(
            portfolio_id=portfolio_id,
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

    history = stock_services.build_ticker_history(db_session, portfolio_id, "AAPL", date(2024, 2, 1), date(2024, 2, 5))
    assert len(history["rows"]) == 5
    assert history["rows"][0]["buy_quantity"] == 2.0
    assert history["summary"]["cumulative_quantity"] == 2.0


@requires_db
def test_asset_endpoints_expose_computed_interest_plan(client, portfolio_id):
    from app.models import Asset, AssetType
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        # computed_interest_plan only runs for a "debt_interest"-typed asset
        # (see asset_net_worth_contribution/computed_interest_plan in
        # main.py) - a "none"-mode asset with the same loan fields filled
        # must NOT get a projection (test_computed_interest_plan_requires_debt_interest_mode
        # below covers that regression explicitly).
        asset_type = AssetType(portfolio_id=portfolio_id, name="Hypotéka", calculation_mode="debt_interest")
        session.add(asset_type)
        session.flush()
        asset = Asset(
            portfolio_id=portfolio_id,
            code="TEST-01",
            name="Testovaci byt",
            asset_type="Byt",
            asset_type_id=asset_type.id,
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
    params = {"portfolio_id": str(portfolio_id)}

    assets_response = client.get("/assets", headers=headers, params=params)
    assert assets_response.status_code == 200
    assert "computed_interest_plan" in assets_response.json()[0]

    projection_response = client.get(f"/assets/{asset_id}/interest-projection", headers=headers, params=params)
    assert projection_response.status_code == 200
    payload = projection_response.json()
    assert payload["total_computed"] > 0
    assert "2026" in payload["computed_plan"]


@requires_db
def test_stocks_alerts_endpoint_requires_auth(client, portfolio_id):
    params = {"portfolio_id": str(portfolio_id)}
    unauthenticated = client.get("/stocks/alerts", params=params)
    assert unauthenticated.status_code == 401

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    response = client.get("/stocks/alerts", headers=headers, params=params)
    assert response.status_code == 200
    assert "watchlist_limit_breaches" in response.json()


@requires_db
def test_recalculate_endpoint_returns_proper_error_instead_of_bare_crash(client, portfolio_id, monkeypatch):
    """Regression test for the "Failed to fetch" report: an unhandled
    exception inside the recalculate endpoint used to reach the browser as a
    bare 500 with no CORS headers (Starlette's outermost error handler sits
    above CORSMiddleware), which browsers report as a CORS failure with no
    hint of the real cause. It must come back as a normal JSON error instead,
    which the TestClient (and a real browser) can read the body of."""
    from app import main

    def boom(db, portfolio_id, dry_run=False, date_from=None, threshold_pct=None):
        raise RuntimeError("simulated crash deep in the recalculation")

    monkeypatch.setattr(main, "recalculate_stocks", boom)

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}", "Origin": "http://localhost:3010"}
    response = client.post(f"/stocks/recalculate?dry_run=true&portfolio_id={portfolio_id}", headers=headers)

    assert response.status_code == 500
    assert "simulated crash" in response.json()["detail"]
    # This is the exact symptom that was reported: a crash reaching the
    # browser with no CORS header shows up there as "blocked by CORS policy"
    # / "Failed to fetch", masking the real 500 underneath.
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3010"


@requires_db
def test_recalculate_stocks_date_from_preserves_earlier_rows(db_session, portfolio_id, monkeypatch):
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
                portfolio_id=portfolio_id,
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
    full = stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False)
    assert full["date_from"] is None
    total_rows_after_full = db_session.scalar(select(func.count()).select_from(DailyStatistic))
    assert total_rows_after_full == full["daily_statistics"]

    jan5_before = db_session.get(DailyStatistic, (portfolio_id, date(2024, 1, 5)))
    assert jan5_before is not None

    # Simulate a manually edited / imported historical row, then confirm an
    # incremental recalculate from a later date leaves it alone.
    jan5_before.total_czk = Decimal("999999")
    db_session.commit()

    date_from = date(2024, 2, 1)
    incremental = stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False, date_from=date_from)
    assert incremental["date_from"] == date_from

    # Incremental recalculate only rewrites dates that already existed - total row count is unchanged.
    total_rows_after_incremental = db_session.scalar(select(func.count()).select_from(DailyStatistic))
    assert total_rows_after_incremental == total_rows_after_full

    jan5_after = db_session.get(DailyStatistic, (portfolio_id, date(2024, 1, 5)))
    assert jan5_after.total_czk == Decimal("999999")  # untouched, before date_from

    feb10_after = db_session.get(DailyStatistic, (portfolio_id, date(2024, 2, 10)))
    assert feb10_after is not None
    # Cumulative total still reflects all three purchases (100+110+120), not just the Feb one.
    assert feb10_after.total_czk == Decimal("330")


@requires_db
def test_recalculate_stocks_ignores_watchlist_tip_and_plan_rows(db_session, portfolio_id, monkeypatch):
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
            portfolio_id=portfolio_id,
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
                portfolio_id=portfolio_id,
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

    stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False)

    position = db_session.get(PortfolioPosition, (portfolio_id, "AAPL"))
    assert position is not None
    assert position.quantity == Decimal("1")
    assert position.invested_czk == Decimal("1000")
    # The real purchase date, not the earlier watchlist/tip/plan row's date.
    assert position.first_buy_date == date(2024, 1, 10)


@requires_db
def test_recalculate_stocks_unrealized_profit_reflects_price_history(db_session, portfolio_id, monkeypatch):
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
            portfolio_id=portfolio_id,
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

    stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False)

    buy_day_row = db_session.get(DailyStatistic, (portfolio_id, buy_date))
    assert buy_day_row is not None
    assert buy_day_row.total_value_czk == Decimal("1000")  # 10 x 100, bought at cost
    assert buy_day_row.unrealized_profit_czk == Decimal("0")

    later_row = db_session.get(DailyStatistic, (portfolio_id, later_date))
    assert later_row is not None
    assert later_row.value_czk == Decimal("1500")  # 10 shares x 150 CZK
    assert later_row.total_value_czk == Decimal("1500")
    assert later_row.invested_czk == Decimal("1000")  # unchanged - still only ever paid 1000
    assert later_row.unrealized_profit_czk == Decimal("500")  # 1500 - 1000, the real price gain


@requires_db
def test_recalculate_stocks_excludes_weekend_fill_days(db_session, portfolio_id, monkeypatch):
    """AkcieStatistika.bas only fills in working days (Po-Pa) between purchases -
    no trading happens on weekends, so Excel never shows a Saturday/Sunday row.
    A weekend day with no transaction must be skipped; a weekend day that DOES
    have a real transaction must still show up (the VBA never filters out
    actual data, only the synthetic empty fill days)."""
    from app import stock_services
    from app.models import DailyStatistic, StockTransaction

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", lambda ticker, date_from, date_to: {"currency": "CZK", "points": []})

    monday = date(2024, 1, 8)
    saturday_with_trade = date(2024, 1, 13)  # unusual but a real transaction landed here
    sunday_empty = date(2024, 1, 14)  # no transaction - must not appear
    next_monday = date(2024, 1, 15)

    for day in (monday, saturday_with_trade, next_monday):
        db_session.add(
            StockTransaction(
                portfolio_id=portfolio_id,
                traded_on=day,
                movement_type="Nákup",
                instrument_name="Test Corp",
                ticker="TEST",
                quantity=Decimal("1"),
                unit_price_ccy=Decimal("100"),
                gross_amount_ccy=Decimal("100"),
                currency="CZK",
                amount_czk=Decimal("100"),
            )
        )
    db_session.commit()

    stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False)

    assert db_session.get(DailyStatistic, (portfolio_id, monday)) is not None  # weekday
    assert db_session.get(DailyStatistic, (portfolio_id, saturday_with_trade)) is not None  # real trade, kept despite weekend
    assert db_session.get(DailyStatistic, (portfolio_id, sunday_empty)) is None  # empty weekend fill day - excluded
    assert db_session.get(DailyStatistic, (portfolio_id, next_monday)) is not None  # weekday


@requires_db
def test_recalculate_stocks_computes_alerts_column(db_session, portfolio_id, monkeypatch):
    """Port of AkcieStatistika.bas's 'Upozorneni' column: a day-over-day price
    move past the threshold, and a ticker with no price data at all, must both
    show up in DailyStatistic.alerts - it used to always be written as None."""
    from app import stock_services
    from app.models import DailyStatistic, StockTransaction

    day1 = date(2024, 1, 8)  # Monday
    day2 = date(2024, 1, 9)  # Tuesday - flat price, no data for NODATA
    day3 = date(2024, 1, 10)  # Wednesday - TEST jumps +30%, still no data for NODATA

    for ticker, price in (("TEST", 100), ("NODATA", 50)):
        db_session.add(
            StockTransaction(
                portfolio_id=portfolio_id,
                traded_on=day1,
                movement_type="Nákup",
                instrument_name=ticker,
                ticker=ticker,
                quantity=Decimal("1"),
                unit_price_ccy=Decimal(price),
                gross_amount_ccy=Decimal(price),
                currency="CZK",
                amount_czk=Decimal(price),
            )
        )
    db_session.commit()

    def fake_history(ticker, date_from, date_to):
        if ticker == "TEST":
            return {"currency": "CZK", "points": [(day1, Decimal("100")), (day2, Decimal("100")), (day3, Decimal("130"))]}
        return {"currency": "CZK", "points": []}  # NODATA: nothing ever comes back from Yahoo

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", fake_history)

    stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False, threshold_pct=Decimal("10"))

    day2_row = db_session.get(DailyStatistic, (portfolio_id, day2))
    assert day2_row is not None
    assert day2_row.alerts == "Chybí cena: NODATA"  # TEST unchanged (0%), below threshold - no mover listed

    day3_row = db_session.get(DailyStatistic, (portfolio_id, day3))
    assert day3_row is not None
    assert "TEST (D:+30.0%)" in day3_row.alerts
    assert "Chybí cena: NODATA" in day3_row.alerts


@requires_db
def test_recalculate_stocks_reports_price_fetch_failures(db_session, portfolio_id, monkeypatch):
    """When Yahoo returns no history at all for a ticker (network outage,
    rate limiting, unknown symbol), that must be visible as a top-level count
    on the recalculate result - not just buried inside per-day alert strings,
    which is easy to miss and previously left market values silently at 0."""
    from app import stock_services
    from app.models import StockTransaction

    day1 = date(2024, 1, 8)  # Monday

    for ticker in ("TEST", "NODATA"):
        db_session.add(
            StockTransaction(
                portfolio_id=portfolio_id,
                traded_on=day1,
                movement_type="Nákup",
                instrument_name=ticker,
                ticker=ticker,
                quantity=Decimal("1"),
                unit_price_ccy=Decimal("100"),
                gross_amount_ccy=Decimal("100"),
                currency="CZK",
                amount_czk=Decimal("100"),
            )
        )
    db_session.commit()

    def fake_history(ticker, date_from, date_to):
        if ticker == "TEST":
            return {"currency": "CZK", "points": [(day1, Decimal("100"))]}
        return {"currency": "CZK", "points": []}

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", fake_history)

    result = stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False)

    assert result["price_fetch_failures"] == 1
    assert result["price_fetch_failed_tickers"] == ["NODATA"]


@requires_db
def test_recalculate_stocks_patches_latest_day_with_live_quote(db_session, portfolio_id, monkeypatch):
    """AkcieStatistika.bas's 'Krok 5b-2': when the daily-candle history has no
    exact close for the latest trading day (Yahoo publishes it late, or the
    recalc runs mid-session), Excel patches that one day in with a live quote
    instead of silently reusing yesterday's close. A ticker already sold off
    entirely must NOT trigger an extra live-quote lookup."""
    from app import stock_services

    today = date.today()
    latest_weekday = today
    while latest_weekday.weekday() >= 5:
        latest_weekday -= timedelta(days=1)
    buy_day = latest_weekday - timedelta(days=14)

    from app.models import StockTransaction

    db_session.add(
        StockTransaction(
            portfolio_id=portfolio_id,
            traded_on=buy_day,
            movement_type="Nákup",
            instrument_name="Held Corp",
            ticker="HELD",
            quantity=Decimal("1"),
            unit_price_ccy=Decimal("100"),
            gross_amount_ccy=Decimal("100"),
            currency="CZK",
            amount_czk=Decimal("100"),
        )
    )
    db_session.add(
        StockTransaction(
            portfolio_id=portfolio_id,
            traded_on=buy_day,
            movement_type="Nákup",
            instrument_name="Sold Corp",
            ticker="SOLD",
            quantity=Decimal("1"),
            unit_price_ccy=Decimal("50"),
            gross_amount_ccy=Decimal("50"),
            currency="CZK",
            amount_czk=Decimal("50"),
        )
    )
    db_session.add(
        StockTransaction(
            portfolio_id=portfolio_id,
            traded_on=buy_day,
            movement_type="Prodej",
            instrument_name="Sold Corp",
            ticker="SOLD",
            quantity=Decimal("-1"),
            unit_price_ccy=Decimal("55"),
            gross_amount_ccy=Decimal("55"),
            currency="CZK",
            amount_czk=Decimal("55"),
        )
    )
    db_session.commit()

    def fake_history(ticker, date_from, date_to):
        # Neither ticker has a candle for the latest weekday - only up to the
        # day before it (simulates a delayed/incomplete publish).
        return {"currency": "CZK", "points": [(buy_day, Decimal("100" if ticker == "HELD" else "50"))]}

    live_quote_calls: list[str] = []

    def fake_price(ticker):
        live_quote_calls.append(ticker)
        return {"price": Decimal("111"), "currency": "CZK"}

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", fake_history)
    monkeypatch.setattr(stock_services, "fetch_yahoo_price", fake_price)

    stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False)

    assert live_quote_calls == ["HELD"]  # SOLD is fully closed out - no extra lookup needed

    from app.models import DailyStatistic

    latest_row = db_session.get(DailyStatistic, (portfolio_id, latest_weekday))
    assert latest_row is not None
    assert latest_row.value_czk == Decimal("111")  # HELD priced via the live-quote patch, not the stale candle


@requires_db
def test_ensure_cnb_rates_up_to_date_fills_missing_weekdays(db_session, monkeypatch):
    """Port of KurzyCNB.bas's FetchCNBIfNeeded(): fetch any missing EUR/USD
    rates between the last stored date and the publish cutoff, skipping
    weekends. Previously nothing called this automatically before a
    recalculate - rates only ever updated via a separate manual step."""
    from app import main
    from app.models import ExchangeRate

    last_stored = date(2024, 1, 5)  # Friday
    for currency, rate in (("EUR", "25.0"), ("USD", "23.0")):
        db_session.add(ExchangeRate(rate_date=last_stored, currency=currency, rate_to_czk=Decimal(rate)))
    db_session.commit()

    # Cutoff a week later so the fill spans a weekend that must be skipped.
    monkeypatch.setattr(main, "cnb_cutoff_date", lambda: date(2024, 1, 12))

    fetched_dates: list[date] = []

    def fake_fetch(rate_date):
        fetched_dates.append(rate_date)
        return [
            {"rate_date": rate_date, "currency": "EUR", "rate_to_czk": Decimal("25.1")},
            {"rate_date": rate_date, "currency": "USD", "rate_to_czk": Decimal("23.1")},
        ]

    monkeypatch.setattr(main, "fetch_cnb_rates", fake_fetch)

    added = main.ensure_cnb_rates_up_to_date(db_session)

    # 2024-01-08 .. 2024-01-12 are Mon-Fri; 01-06/01-07 (Sat/Sun) must be skipped.
    assert fetched_dates == [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11), date(2024, 1, 12)]
    assert added == 10  # 5 days x 2 currencies

    from sqlalchemy import select as sa_select

    friday_rate = db_session.scalar(
        sa_select(ExchangeRate).where(ExchangeRate.rate_date == date(2024, 1, 12), ExchangeRate.currency == "EUR")
    )
    assert friday_rate is not None
    assert friday_rate.rate_to_czk == Decimal("25.1")


@requires_db
def test_ensure_cnb_rates_up_to_date_skips_when_history_empty(db_session, monkeypatch):
    """Matches the VBA's own guard: an empty rates table is left alone rather
    than triggering a bulk backfill as a side effect of a routine recompute."""
    from app import main

    called = False

    def fake_fetch(rate_date):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main, "fetch_cnb_rates", fake_fetch)

    added = main.ensure_cnb_rates_up_to_date(db_session)

    assert added == 0
    assert called is False


def test_fetch_cnb_rates_skips_malformed_lines_instead_of_raising(monkeypatch):
    """A single unexpected/malformed line in CNB's daily rates feed (odd
    formatting, a withdrawn currency, a zero amount) must not blow up the
    whole request - previously an uncaught Decimal/ZeroDivisionError here
    surfaced to the browser as a bare 500 with no CORS headers, which shows
    up misleadingly as "Failed to fetch" instead of a real error message."""
    from app import main

    cnb_text = (
        "země|měna|množství|kód|kurz\n"
        "EMU|euro|1|EUR|25,150\n"
        "garbled|line|not|enough\n"  # too few columns - already skipped before
        "USA|dolar|1|USD|0|not-a-number\n"  # amount is 0 -> ZeroDivisionError if unhandled
        "Some|thing|1|XXX|not-a-decimal\n"  # rate isn't a valid Decimal
        "Japonsko|jen|100|JPY|15,234\n"
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return cnb_text.encode("windows-1250")

    monkeypatch.setattr(main, "urlopen", lambda url, timeout=15: FakeResponse())

    rows = main.fetch_cnb_rates(date(2024, 1, 8))

    # Only the two well-formed lines survive - the malformed ones are skipped,
    # not raised.
    currencies = {row["currency"] for row in rows}
    assert currencies == {"EUR", "JPY"}


@requires_db
def test_ensure_cnb_rates_up_to_date_survives_one_bad_day(db_session, monkeypatch):
    """If fetch_cnb_rates raises for one day in the range (network hiccup,
    unexpected format), the remaining days must still get filled in - a
    single bad day must not take down the whole recalculation."""
    from app import main
    from app.models import ExchangeRate

    last_stored = date(2024, 1, 8)  # Monday
    for currency, rate in (("EUR", "25.0"), ("USD", "23.0")):
        db_session.add(ExchangeRate(rate_date=last_stored, currency=currency, rate_to_czk=Decimal(rate)))
    db_session.commit()

    monkeypatch.setattr(main, "cnb_cutoff_date", lambda: date(2024, 1, 10))

    def flaky_fetch(rate_date):
        if rate_date == date(2024, 1, 9):
            raise ValueError("unexpected CNB response shape")
        return [
            {"rate_date": rate_date, "currency": "EUR", "rate_to_czk": Decimal("25.1")},
            {"rate_date": rate_date, "currency": "USD", "rate_to_czk": Decimal("23.1")},
        ]

    monkeypatch.setattr(main, "fetch_cnb_rates", flaky_fetch)

    added = main.ensure_cnb_rates_up_to_date(db_session)  # must not raise

    assert added == 2  # only 2024-01-10 succeeded (2 currencies); 01-09 was skipped


@requires_db
def test_import_loans_skips_baked_in_subtotal_rows(db_session, portfolio_id):
    """The source 'Půjčky Pohyby' sheet has monthly/yearly subtotal rows
    physically sitting between the real movements (column A holds a text
    label like "Leden 2023" or "2023" instead of a date, lender/borrower
    blank). These must not be imported as if they were real loan movements -
    the app computes its own collapsible subtotals instead."""
    from openpyxl import Workbook

    from app.excel_import import import_loans
    from app.models import LoanMovement

    wb = Workbook()
    ws = wb.active
    ws.title = "Půjčky Pohyby"
    ws.append(["Datum", "Věřitel", "Dlužník", "Částka", "Úrok", "Perioda úroku", "Plán ukončení", "Ukončeno", "Popis"])
    ws.append([datetime(2023, 1, 15), "Martin Ševčík", "Magda Havlíková", 54139, None, None, None, None, None])
    ws.append(["Leden 2023", None, None, 54139, None, None, None, None, None])  # month subtotal - must be skipped
    ws.append([datetime(2023, 3, 1), "TANAKA, s.r.o.", "ACE-TECH, s.r.o.", 7000000, 0.06, "měsíčně", None, None, None])
    ws.append(["2023", None, None, 7054139, None, None, None, None, None])  # year subtotal - must be skipped

    count = import_loans(db_session, wb, portfolio_id)
    db_session.commit()

    assert count == 2  # only the two real movements
    rows = db_session.scalars(select(LoanMovement)).all()
    assert len(rows) == 2
    assert all(row.movement_date is not None for row in rows)
    assert {row.amount for row in rows} == {Decimal("54139"), Decimal("7000000")}


@requires_db
def test_cleanup_loan_subtotals_endpoint_removes_only_dateless_rows(client, db_session, portfolio_id):
    """Regression cleanup path for databases imported before the fix: a
    dateless LoanMovement (the old subtotal artifact) must be deleted, a
    real dated movement must survive."""
    from app.models import LoanMovement

    db_session.add(LoanMovement(portfolio_id=portfolio_id, movement_date=None, amount=Decimal("54139"), source_row=3))
    db_session.add(
        LoanMovement(portfolio_id=portfolio_id, movement_date=date(2023, 1, 15), amount=Decimal("54139"), source_row=2)
    )
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    response = client.post(
        "/loans/cleanup-imported-subtotals", headers=headers, params={"portfolio_id": str(portfolio_id)}
    )

    assert response.status_code == 200
    assert response.json()["deleted"] == 1

    remaining = db_session.scalars(select(LoanMovement)).all()
    assert len(remaining) == 1
    assert remaining[0].movement_date == date(2023, 1, 15)


@requires_db
def test_two_factor_setup_confirm_and_login_flow(client, portfolio_id):
    """End-to-end 2FA enrollment + login: setup returns a scannable secret,
    confirm requires a real TOTP code (not just any string), and once
    enabled, plain username+password login stops short of a full token until
    the pending token is redeemed with a correct code."""
    import pyotp

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    assert login.json()["requires_2fa"] is False
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    setup = client.post("/auth/2fa/setup", headers=headers)
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert setup.json()["qr_code_png_base64"]

    bad_confirm = client.post("/auth/2fa/confirm", headers=headers, json={"secret": secret, "code": "000000"})
    assert bad_confirm.status_code == 400

    code = pyotp.TOTP(secret).now()
    confirm = client.post("/auth/2fa/confirm", headers=headers, json={"secret": secret, "code": code})
    assert confirm.status_code == 200
    assert confirm.json()["totp_enabled"] is True

    me = client.get("/auth/me", headers=headers)
    assert me.json()["totp_enabled"] is True

    second_login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    assert second_login.status_code == 200
    body = second_login.json()
    assert body["requires_2fa"] is True
    assert "token" not in body
    pending_token = body["pending_token"]

    # The pending token alone must not grant access to a real endpoint.
    params = {"portfolio_id": str(portfolio_id)}
    pending_headers = {"Authorization": f"Bearer {pending_token}"}
    denied = client.get("/summary", headers=pending_headers, params=params)
    assert denied.status_code == 401

    wrong_code = client.post("/auth/2fa/login", json={"pending_token": pending_token, "code": "000000"})
    assert wrong_code.status_code == 401

    fresh_code = pyotp.TOTP(secret).now()
    finish = client.post("/auth/2fa/login", json={"pending_token": pending_token, "code": fresh_code})
    assert finish.status_code == 200
    full_token = finish.json()["token"]

    full_headers = {"Authorization": f"Bearer {full_token}"}
    ok = client.get("/summary", headers=full_headers, params=params)
    assert ok.status_code == 200

    # Disable requires the correct password + a valid current code.
    disable_wrong_password = client.post(
        "/auth/2fa/disable", headers=full_headers, json={"password": "wrong", "code": pyotp.TOTP(secret).now()}
    )
    assert disable_wrong_password.status_code == 401

    disable = client.post(
        "/auth/2fa/disable", headers=full_headers, json={"password": "finance", "code": pyotp.TOTP(secret).now()}
    )
    assert disable.status_code == 200
    assert disable.json()["totp_enabled"] is False

    plain_login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    assert plain_login.json()["requires_2fa"] is False
    assert "token" in plain_login.json()


@requires_db
def test_admin_reset_2fa_endpoint_clears_lost_device_lockout(client, db_session):
    """Recovery path: an admin can force another user's 2FA back off (no
    email/SMS backup exists), unblocking a user who lost their device."""
    import pyotp

    from app.auth import hash_password
    from app.models import AppUser

    db_session.add(
        AppUser(
            username="analyst",
            password_hash=hash_password("s3cret!"),
            full_name="Analyst",
            is_active=True,
            is_admin=False,
            allowed_agendas=["portfolio"],
        )
    )
    db_session.commit()

    admin_login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    analyst_login = client.post("/auth/login", json={"username": "analyst", "password": "s3cret!"})
    analyst_headers = {"Authorization": f"Bearer {analyst_login.json()['token']}"}
    setup = client.post("/auth/2fa/setup", headers=analyst_headers).json()
    code = pyotp.TOTP(setup["secret"]).now()
    client.post("/auth/2fa/confirm", headers=analyst_headers, json={"secret": setup["secret"], "code": code})

    locked_out_login = client.post("/auth/login", json={"username": "analyst", "password": "s3cret!"})
    assert locked_out_login.json()["requires_2fa"] is True

    reset = client.post("/users/analyst/2fa/reset", headers=admin_headers)
    assert reset.status_code == 200
    assert reset.json()["totp_enabled"] is False

    recovered_login = client.post("/auth/login", json={"username": "analyst", "password": "s3cret!"})
    assert recovered_login.json()["requires_2fa"] is False

    # A non-admin cannot reset anyone's 2FA.
    forbidden = client.post("/users/admin/2fa/reset", headers=analyst_headers)
    assert forbidden.status_code == 403


@requires_db
def test_notification_settings_are_saved_per_user_and_used_as_alert_default(client, db_session, portfolio_id):
    """Nastaveni tab: a user's own daily-change/drop thresholds persist via
    /auth/me, and /stocks/alerts falls back to the saved drop threshold when
    the caller doesn't pass an explicit threshold_pct."""
    from app.models import PortfolioPosition

    db_session.add(
        PortfolioPosition(
            portfolio_id=portfolio_id,
            ticker="TST",
            name="Test Corp",
            quantity=Decimal("10"),
            current_price=Decimal("90"),
            currency="CZK",
            market_value_czk=Decimal("900"),
            invested_czk=Decimal("1000"),
            profit_czk=Decimal("-100"),
            profit_pct=Decimal("-0.10"),
        )
    )
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    me = client.get("/auth/me", headers=headers)
    assert me.json()["alert_daily_change_pct"] is None
    assert me.json()["alert_drop_pct"] is None

    invalid = client.put("/auth/me/notification-settings", headers=headers, json={"alert_drop_pct": 150})
    assert invalid.status_code == 400

    saved = client.put(
        "/auth/me/notification-settings",
        headers=headers,
        json={"alert_daily_change_pct": 5, "alert_drop_pct": 8},
    )
    assert saved.status_code == 200
    assert saved.json()["alert_daily_change_pct"] == 5
    assert saved.json()["alert_drop_pct"] == 8

    me_after = client.get("/auth/me", headers=headers)
    assert me_after.json()["alert_drop_pct"] == 8

    # -10% drawdown breaches an 8% saved threshold but not the 10% app default.
    alerts_default = client.get("/stocks/alerts", headers=headers, params={"portfolio_id": str(portfolio_id)})
    assert any(row["ticker"] == "TST" for row in alerts_default.json()["portfolio_drawdowns"])

    alerts_explicit = client.get(
        "/stocks/alerts", headers=headers, params={"portfolio_id": str(portfolio_id), "threshold_pct": 50}
    )
    assert not any(row["ticker"] == "TST" for row in alerts_explicit.json()["portfolio_drawdowns"])


@requires_db
def test_portfolio_scoping_isolates_data_between_subjekty(client, db_session):
    """Core guarantee of the Subjekt (Portfolio) feature: a user granted
    access to only one Subjekt/one agenda cannot read another Subjekt's data,
    nor an agenda they weren't granted within their own Subjekt - enforced
    server-side, not just hidden client-side."""
    from app.auth import hash_password
    from app.models import Asset, AppUser, Portfolio, PortfolioAccess

    portfolio_a = Portfolio(name="Subjekt A")
    portfolio_b = Portfolio(name="Subjekt B")
    db_session.add_all([portfolio_a, portfolio_b])
    db_session.flush()

    db_session.add(Asset(portfolio_id=portfolio_a.id, code="A-01", name="Byt A"))
    db_session.add(Asset(portfolio_id=portfolio_b.id, code="B-01", name="Byt B"))
    db_session.add(
        AppUser(username="scoped", password_hash=hash_password("s3cret!"), is_active=True, is_admin=False, allowed_agendas=[])
    )
    db_session.add(PortfolioAccess(username="scoped", portfolio_id=portfolio_a.id, allowed_agendas=["assets"]))
    db_session.commit()

    login = client.post("/auth/login", json={"username": "scoped", "password": "s3cret!"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    granted = client.get("/assets", headers=headers, params={"portfolio_id": str(portfolio_a.id)})
    assert granted.status_code == 200
    assert [row["code"] for row in granted.json()] == ["A-01"]

    other_subjekt = client.get("/assets", headers=headers, params={"portfolio_id": str(portfolio_b.id)})
    assert other_subjekt.status_code == 403

    ungranted_agenda = client.get("/loans/movements", headers=headers, params={"portfolio_id": str(portfolio_a.id)})
    assert ungranted_agenda.status_code == 403


@requires_db
def test_recalculate_stocks_keeps_same_ticker_separate_across_subjekty(db_session, monkeypatch):
    """The compound (portfolio_id, ticker) primary key on PortfolioPosition
    must actually keep two Subjekty's positions independent - this is the
    concrete risk that migrating off the old ticker-only primary key was
    meant to fix."""
    from app import stock_services
    from app.models import Portfolio, PortfolioPosition, StockTransaction

    portfolio_a = Portfolio(name="Subjekt A")
    portfolio_b = Portfolio(name="Subjekt B")
    db_session.add_all([portfolio_a, portfolio_b])
    db_session.flush()

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", lambda ticker, date_from, date_to: {"currency": "CZK", "points": []})

    for portfolio, qty in ((portfolio_a, 1), (portfolio_b, 5)):
        db_session.add(
            StockTransaction(
                portfolio_id=portfolio.id,
                traded_on=date(2024, 1, 10),
                movement_type="Nákup",
                instrument_name="Apple",
                ticker="AAPL",
                quantity=Decimal(qty),
                unit_price_ccy=Decimal("100"),
                gross_amount_ccy=Decimal(100 * qty),
                currency="CZK",
                amount_czk=Decimal(100 * qty),
            )
        )
    db_session.commit()

    stock_services.recalculate_stocks(db_session, portfolio_a.id, dry_run=False)
    stock_services.recalculate_stocks(db_session, portfolio_b.id, dry_run=False)

    position_a = db_session.get(PortfolioPosition, (portfolio_a.id, "AAPL"))
    position_b = db_session.get(PortfolioPosition, (portfolio_b.id, "AAPL"))
    assert position_a is not None and position_b is not None
    assert position_a.quantity == Decimal("1")
    assert position_b.quantity == Decimal("5")  # not 6 - the two Subjekty never merge


@requires_db
def test_admin_sees_every_portfolio_without_explicit_grants(client, db_session):
    """Admins bypass PortfolioAccess entirely (mirrors the existing
    admin-bypasses-allowed_agendas behaviour) - no explicit grant row is
    needed for an admin to reach every Subjekt."""
    from app.models import Portfolio

    portfolio_a = Portfolio(name="Subjekt A")
    portfolio_b = Portfolio(name="Subjekt B")
    db_session.add_all([portfolio_a, portfolio_b])
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    for portfolio in (portfolio_a, portfolio_b):
        response = client.get("/assets", headers=headers, params={"portfolio_id": str(portfolio.id)})
        assert response.status_code == 200

    me = client.get("/auth/me", headers=headers)
    portfolio_ids = {row["id"] for row in me.json()["portfolios"]}
    assert {str(portfolio_a.id), str(portfolio_b.id)}.issubset(portfolio_ids)
    assert all(row["allowed_agendas"] for row in me.json()["portfolios"])


@requires_db
def test_portfolio_access_bulk_replace_round_trips_through_auth_me(client, db_session):
    """PUT /users/{username}/portfolio-access bulk-replaces a user's full
    grant set (like POST /users already does for allowed_agendas) - a second,
    smaller call must replace, not merge with, the first."""
    from app.auth import hash_password
    from app.models import AppUser, Portfolio

    portfolio_a = Portfolio(name="Subjekt A")
    portfolio_b = Portfolio(name="Subjekt B")
    db_session.add_all([portfolio_a, portfolio_b])
    db_session.add(
        AppUser(username="scoped", password_hash=hash_password("s3cret!"), is_active=True, is_admin=False, allowed_agendas=[])
    )
    db_session.commit()

    admin_login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

    grant_both = client.put(
        f"/users/scoped/portfolio-access",
        headers=admin_headers,
        json={
            "grants": [
                {"portfolio_id": str(portfolio_a.id), "allowed_agendas": ["assets", "loans"]},
                {"portfolio_id": str(portfolio_b.id), "allowed_agendas": ["transactions"]},
            ]
        },
    )
    assert grant_both.status_code == 200

    scoped_login = client.post("/auth/login", json={"username": "scoped", "password": "s3cret!"})
    scoped_headers = {"Authorization": f"Bearer {scoped_login.json()['token']}"}
    me_after_first = client.get("/auth/me", headers=scoped_headers).json()
    assert {row["id"]: row["allowed_agendas"] for row in me_after_first["portfolios"]} == {
        str(portfolio_a.id): ["assets", "loans"],
        str(portfolio_b.id): ["transactions"],
    }

    grant_one = client.put(
        f"/users/scoped/portfolio-access",
        headers=admin_headers,
        json={"grants": [{"portfolio_id": str(portfolio_a.id), "allowed_agendas": ["assets"]}]},
    )
    assert grant_one.status_code == 200

    me_after_second = client.get("/auth/me", headers=scoped_headers).json()
    assert {row["id"]: row["allowed_agendas"] for row in me_after_second["portfolios"]} == {str(portfolio_a.id): ["assets"]}


@requires_db
def test_update_user_endpoint_edits_profile_and_login_still_works(client, db_session):
    """PUT /users/{username}: full_name/is_admin/is_active/allowed_agendas
    (global) are editable, password resets when provided (and login then
    requires the NEW password), and an admin can't strip their own
    is_admin/is_active (would be a self-lockout with no recovery path)."""
    from app.auth import hash_password
    from app.models import AppUser

    db_session.add(
        AppUser(username="editable", password_hash=hash_password("orig-pass"), full_name="Original Name", is_active=True, is_admin=False, allowed_agendas=[])
    )
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    updated = client.put(
        "/users/editable",
        headers=headers,
        json={
            "full_name": "New Name",
            "is_admin": False,
            "is_active": True,
            "allowed_agendas": ["rates", "subjects"],
            "password": "new-pass",
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["full_name"] == "New Name"
    assert body["allowed_agendas"] == ["rates", "subjects"]

    # Old password no longer works, new one does.
    old_login = client.post("/auth/login", json={"username": "editable", "password": "orig-pass"})
    assert old_login.status_code == 401
    new_login = client.post("/auth/login", json={"username": "editable", "password": "new-pass"})
    assert new_login.status_code == 200

    # Editing without a password leaves the (new) password untouched.
    no_password_change = client.put(
        "/users/editable",
        headers=headers,
        json={"full_name": "New Name", "is_admin": False, "is_active": True, "allowed_agendas": []},
    )
    assert no_password_change.status_code == 200
    still_works = client.post("/auth/login", json={"username": "editable", "password": "new-pass"})
    assert still_works.status_code == 200

    # Admin can't remove their own admin/active status via this endpoint.
    self_lockout = client.put(
        "/users/admin", headers=headers, json={"full_name": "Administrátor", "is_admin": False, "is_active": True, "allowed_agendas": []}
    )
    assert self_lockout.status_code == 400


@requires_db
def test_cost_categories_admin_only_write_scoped_read(client, db_session, portfolio_id):
    """GET requires the "categories" agenda (granted per-Subjekt); POST/DELETE
    require admin regardless of that grant. Names are unique per Subjekt."""
    from app.auth import hash_password
    from app.models import AppUser, PortfolioAccess

    db_session.add(
        AppUser(username="reader", password_hash=hash_password("s3cret!"), is_active=True, is_admin=False, allowed_agendas=[])
    )
    db_session.add(PortfolioAccess(username="reader", portfolio_id=portfolio_id, allowed_agendas=["categories"]))
    db_session.commit()

    admin_login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}
    reader_login = client.post("/auth/login", json={"username": "reader", "password": "s3cret!"})
    reader_headers = {"Authorization": f"Bearer {reader_login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    created = client.post("/assets/cost-categories", headers=admin_headers, params=params, json={"name": "Energie"})
    assert created.status_code == 200
    category_id = created.json()["id"]

    duplicate = client.post("/assets/cost-categories", headers=admin_headers, params=params, json={"name": "Energie"})
    assert duplicate.status_code == 409

    # Reader has the read grant but is not admin - can list, can't write.
    listed = client.get("/assets/cost-categories", headers=reader_headers, params=params)
    assert listed.status_code == 200
    assert [row["name"] for row in listed.json()] == ["Energie"]

    forbidden_create = client.post("/assets/cost-categories", headers=reader_headers, params=params, json={"name": "Voda"})
    assert forbidden_create.status_code == 403
    forbidden_delete = client.delete(f"/assets/cost-categories/{category_id}", headers=reader_headers)
    assert forbidden_delete.status_code == 403

    deleted = client.delete(f"/assets/cost-categories/{category_id}", headers=admin_headers)
    assert deleted.status_code == 200
    listed_after = client.get("/assets/cost-categories", headers=admin_headers, params=params)
    assert listed_after.json() == []


@requires_db
def test_asset_cost_crud_resolves_category_and_payer_and_is_portfolio_scoped(client, db_session, portfolio_id):
    """POST/PUT resolve free-text category/payer via get-or-create (so the
    dictionary is reused, not duplicated), and every read/write is 404'd once
    scoped to a portfolio_id the cost doesn't belong to."""
    from app.models import Portfolio

    other_portfolio = Portfolio(name="Jiny Subjekt")
    db_session.add(other_portfolio)
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    created = client.post(
        "/assets/costs",
        headers=headers,
        params=params,
        json={"item": "Pojistka", "category": "Pojisteni", "amount": "1234.50", "payer": "Martin"},
    )
    assert created.status_code == 200
    body = created.json()
    cost_id = body["id"]
    assert body["category"] == "Pojisteni"
    assert body["payer"] == "Martin"
    assert body["has_attachment"] is False

    # A second cost reusing the same category/payer text must resolve to the
    # same dictionary rows, not create duplicates.
    from app.models import CostCategory, Party

    assert db_session.query(CostCategory).filter_by(portfolio_id=portfolio_id, name="Pojisteni").count() == 1
    assert db_session.query(Party).filter_by(name="Martin").count() == 1

    listed = client.get("/assets/costs", headers=headers, params=params)
    assert listed.status_code == 200
    assert [row["item"] for row in listed.json()] == ["Pojistka"]

    updated = client.put(
        f"/assets/costs/{cost_id}",
        headers=headers,
        params=params,
        json={"item": "Pojistka upravena", "category": "Pojisteni", "amount": "999"},
    )
    assert updated.status_code == 200
    assert updated.json()["item"] == "Pojistka upravena"
    assert updated.json()["payer"] is None  # PUT payload omitted payer - it's cleared, not left untouched.

    # Scoped to the wrong Subjekt, the same cost is invisible.
    other_params = {"portfolio_id": str(other_portfolio.id)}
    wrong_scope_list = client.get("/assets/costs", headers=headers, params=other_params)
    assert wrong_scope_list.json() == []
    wrong_scope_update = client.put(
        f"/assets/costs/{cost_id}", headers=headers, params=other_params, json={"item": "x"}
    )
    assert wrong_scope_update.status_code == 404
    wrong_scope_delete = client.delete(f"/assets/costs/{cost_id}", headers=headers, params=other_params)
    assert wrong_scope_delete.status_code == 404

    deleted = client.delete(f"/assets/costs/{cost_id}", headers=headers, params=params)
    assert deleted.status_code == 200
    listed_after = client.get("/assets/costs", headers=headers, params=params)
    assert listed_after.json() == []


@requires_db
def test_asset_cost_create_rejects_asset_from_another_portfolio(client, db_session, portfolio_id):
    from app.models import Asset, Portfolio

    other_portfolio = Portfolio(name="Jiny Subjekt")
    db_session.add(other_portfolio)
    db_session.commit()
    foreign_asset = Asset(portfolio_id=other_portfolio.id, code="X-1", name="Cizi majetek")
    db_session.add(foreign_asset)
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    response = client.post(
        "/assets/costs",
        headers=headers,
        params={"portfolio_id": str(portfolio_id)},
        json={"item": "Test", "asset_id": str(foreign_asset.id)},
    )
    assert response.status_code == 404


@requires_db
def test_asset_cost_attachment_upload_download_delete(client, db_session, portfolio_id, tmp_path, monkeypatch):
    """Attachments are stored as {cost.id}.pdf under ATTACHMENTS_DIR, gated by
    a magic-bytes check (Content-Type is client-supplied and spoofable)."""
    from app import main as main_module

    monkeypatch.setattr(main_module.settings, "attachments_dir", str(tmp_path))

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    created = client.post("/assets/costs", headers=headers, params=params, json={"item": "Faktura"})
    cost_id = created.json()["id"]

    not_a_pdf = client.post(
        f"/assets/costs/{cost_id}/attachment",
        headers=headers,
        params=params,
        files={"file": ("fake.pdf", b"not actually a pdf", "application/pdf")},
    )
    assert not_a_pdf.status_code == 400

    pdf_bytes = b"%PDF-1.4\n%%EOF"
    uploaded = client.post(
        f"/assets/costs/{cost_id}/attachment",
        headers=headers,
        params=params,
        files={"file": ("real.pdf", pdf_bytes, "application/pdf")},
    )
    assert uploaded.status_code == 200
    assert (tmp_path / f"{cost_id}.pdf").read_bytes() == pdf_bytes

    listed = client.get("/assets/costs", headers=headers, params=params)
    assert listed.json()[0]["has_attachment"] is True

    downloaded = client.get(f"/assets/costs/{cost_id}/attachment", headers=headers, params=params)
    assert downloaded.status_code == 200
    assert downloaded.content == pdf_bytes

    deleted = client.delete(f"/assets/costs/{cost_id}/attachment", headers=headers, params=params)
    assert deleted.status_code == 200
    assert not (tmp_path / f"{cost_id}.pdf").exists()

    after_delete = client.get(f"/assets/costs/{cost_id}/attachment", headers=headers, params=params)
    assert after_delete.status_code == 404


@requires_db
def test_asset_cost_write_requires_costs_agenda_grant(client, db_session, portfolio_id):
    from app.auth import hash_password
    from app.models import AppUser, PortfolioAccess

    db_session.add(
        AppUser(username="viewer", password_hash=hash_password("s3cret!"), is_active=True, is_admin=False, allowed_agendas=[])
    )
    # Granted "assets" but not "costs" - can't touch cost endpoints even
    # though they're on the same Subjekt.
    db_session.add(PortfolioAccess(username="viewer", portfolio_id=portfolio_id, allowed_agendas=["assets"]))
    db_session.commit()

    login = client.post("/auth/login", json={"username": "viewer", "password": "s3cret!"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    response = client.post("/assets/costs", headers=headers, params=params, json={"item": "Test"})
    assert response.status_code == 403
