from __future__ import annotations

import bisect
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .models import DailyStatistic, ExchangeRate, PortfolioPosition, StockTransaction, TickerDescription, WatchlistStock


ZERO = Decimal("0")
ONE = Decimal("1")

YAHOO_SUFFIXES = {
    "XETR": ".DE",
    "XPAR": ".PA",
    "XAMS": ".AS",
    "XLON": ".L",
    "XMIL": ".MI",
    "XMAD": ".MC",
    "XHEL": ".HE",
    "XSTO": ".ST",
    "XCSE": ".CO",
    "XOSL": ".OL",
    "XSWX": ".SW",
    "XWIEN": ".VI",
    "XWBO": ".VI",
    "WBAG": ".VI",
    "XIST": ".IS",
    "XHKG": ".HK",
    "XTSE": ".TO",
    "XASX": ".AX",
    "XJPX": ".T",
}


@dataclass
class PatriaTrade:
    traded_on: date
    quantity: Decimal
    movement_type: str
    instrument_name: str | None
    fee_ccy: Decimal
    market: str | None
    unit_price_ccy: Decimal
    isin: str | None
    gross_amount_ccy: Decimal
    currency: str


def to_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def decimal_or_zero(value: Any) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return ZERO
    text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return ZERO


def as_decimal(value: Any) -> Decimal | None:
    result = decimal_or_zero(value)
    return result if result != ZERO else None


def parse_date(value: str) -> date | None:
    text = value.strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def rate_for_day(db: Session, currency: str | None, target_date: date | None) -> Decimal:
    if not currency or currency.upper() == "CZK" or target_date is None:
        return ONE
    row = db.scalar(
        select(ExchangeRate)
        .where(ExchangeRate.currency == currency.upper(), ExchangeRate.rate_date <= target_date)
        .order_by(desc(ExchangeRate.rate_date))
        .limit(1)
    )
    return row.rate_to_czk if row else ONE


def latest_rate(db: Session, currency: str | None) -> Decimal:
    if not currency or currency.upper() == "CZK":
        return ONE
    row = db.scalar(
        select(ExchangeRate)
        .where(ExchangeRate.currency == currency.upper())
        .order_by(desc(ExchangeRate.rate_date))
        .limit(1)
    )
    return row.rate_to_czk if row else ONE


def normalize_ticker(ticker: str | None, market: str | None = None) -> str | None:
    if not ticker:
        return None
    value = ticker.strip().upper()
    if "." in value:
        return value
    suffix = YAHOO_SUFFIXES.get((market or "").strip().upper())
    return f"{value}{suffix}" if suffix else value


def ticker_from_existing_data(db: Session, isin: str | None, name: str | None, market: str | None) -> str | None:
    if isin:
        description = db.scalar(select(TickerDescription).where(TickerDescription.isin == isin).limit(1))
        if description and description.ticker:
            return normalize_ticker(description.ticker, market)
        transaction = db.scalar(
            select(StockTransaction)
            .where(StockTransaction.isin == isin, StockTransaction.ticker.is_not(None))
            .order_by(desc(StockTransaction.traded_on).nullslast())
            .limit(1)
        )
        if transaction and transaction.ticker:
            return normalize_ticker(transaction.ticker, market)
    if name:
        transaction = db.scalar(
            select(StockTransaction)
            .where(StockTransaction.instrument_name == name, StockTransaction.ticker.is_not(None))
            .order_by(desc(StockTransaction.traded_on).nullslast())
            .limit(1)
        )
        if transaction and transaction.ticker:
            return normalize_ticker(transaction.ticker, market)
    return None


def parse_patria_text(text: str) -> list[PatriaTrade]:
    lines = [line for line in text.splitlines() if line.strip()]
    trades: list[PatriaTrade] = []
    index = 0
    while index + 1 < len(lines):
        first = [part.strip() for part in lines[index].split("\t")]
        second = [part.strip() for part in lines[index + 1].split("\t")]
        index += 2

        offset = 0
        if first and not parse_date(first[0]) and len(first) > 1 and parse_date(first[1]):
            offset = 1
        if len(first) <= offset + 6 or len(second) <= offset + 6:
            continue
        traded_on = parse_date(first[offset])
        if traded_on is None:
            continue

        quantity = decimal_or_zero(first[offset + 1])
        direction = first[offset + 2].strip().lower()
        movement_type = "Prodej" if "prodej" in direction or "sell" in direction else "Nákup"
        if movement_type == "Prodej":
            quantity = -abs(quantity)
        else:
            quantity = abs(quantity)

        unit_price = decimal_or_zero(second[offset + 1])
        total = decimal_or_zero(second[offset + 5])
        if total == ZERO:
            total = abs(quantity) * unit_price

        trades.append(
            PatriaTrade(
                traded_on=traded_on,
                quantity=quantity,
                movement_type=movement_type,
                instrument_name=first[offset + 3] or None,
                fee_ccy=decimal_or_zero(first[offset + 4]) + decimal_or_zero(second[offset + 4]),
                market=first[offset + 6] or None,
                unit_price_ccy=unit_price,
                isin=second[offset + 3] or None,
                gross_amount_ccy=total,
                currency=(second[offset + 6] or "CZK").upper(),
            )
        )
    return trades


def import_patria_trades(db: Session, text: str) -> dict[str, Any]:
    trades = parse_patria_text(text)
    inserted = 0
    skipped = 0
    for trade in trades:
        duplicate = db.scalar(
            select(StockTransaction)
            .where(
                StockTransaction.traded_on == trade.traded_on,
                StockTransaction.instrument_name == trade.instrument_name,
                StockTransaction.quantity == trade.quantity,
                StockTransaction.unit_price_ccy == trade.unit_price_ccy,
            )
            .limit(1)
        )
        if duplicate:
            skipped += 1
            continue
        rate = rate_for_day(db, trade.currency, trade.traded_on)
        fee_czk = trade.fee_ccy * rate
        amount_czk = trade.gross_amount_ccy * rate + fee_czk
        ticker = ticker_from_existing_data(db, trade.isin, trade.instrument_name, trade.market)
        db.add(
            StockTransaction(
                traded_on=trade.traded_on,
                instrument_type="Akcie",
                movement_type=trade.movement_type,
                instrument_name=trade.instrument_name,
                isin=trade.isin,
                ticker=ticker,
                market=trade.market,
                quantity=trade.quantity,
                unit_price_ccy=trade.unit_price_ccy,
                gross_amount_ccy=trade.gross_amount_ccy,
                currency=trade.currency,
                fee_ccy=trade.fee_ccy,
                fee_czk=fee_czk,
                amount_czk=amount_czk,
                description="Import Patria",
            )
        )
        inserted += 1
    db.commit()
    return {"parsed": len(trades), "inserted": inserted, "skipped_duplicates": skipped}


def fetch_yahoo_price(ticker: str) -> dict[str, Decimal | str | None]:
    symbol = quote(ticker)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
    ]
    headers = {"User-Agent": "FinanceSEMA/1.0"}
    for url in urls:
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
            result = payload["chart"]["result"][0]
            meta = result.get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            currency = meta.get("currency")
            if price is not None:
                return {"price": decimal_or_zero(price), "currency": currency}
        except Exception:
            continue
    return {"price": None, "currency": None}


def refresh_current_prices(db: Session, threshold_pct: Decimal | float = Decimal("10")) -> dict[str, Any]:
    threshold = float(threshold_pct)
    positions = db.scalars(select(PortfolioPosition).where(PortfolioPosition.ticker.is_not(None))).all()
    watchlist = db.scalars(select(WatchlistStock).where(WatchlistStock.ticker.is_not(None))).all()
    tickers = sorted(
        {
            *{normalize_ticker(position.ticker, None) for position in positions if position.ticker},
            *{normalize_ticker(watch.ticker, None) for watch in watchlist if watch.ticker},
        }
    )
    updated = 0
    errors: list[dict[str, str]] = []
    movers: list[dict[str, Any]] = []
    for ticker in tickers:
        if not ticker:
            continue
        quote_data = fetch_yahoo_price(ticker)
        price = quote_data["price"]
        if not isinstance(price, Decimal):
            errors.append({"ticker": ticker, "error": "Cena nebyla dostupná"})
            continue
        position = db.get(PortfolioPosition, ticker)
        previous_price = position.current_price if position is not None else None
        rate_currency = str(quote_data["currency"]).upper() if quote_data.get("currency") else None
        currency_for_rate = rate_currency or (position.currency if position is not None else None)
        if not currency_for_rate:
            latest_transaction = db.scalar(
                select(StockTransaction)
                .where(StockTransaction.ticker == ticker, StockTransaction.currency.is_not(None))
                .order_by(desc(StockTransaction.traded_on).nullslast())
                .limit(1)
            )
            currency_for_rate = latest_transaction.currency if latest_transaction else None
        rate = latest_rate(db, currency_for_rate)
        if position is not None:
            position.current_price = price
            if rate_currency:
                position.currency = rate_currency
            quantity = decimal_or_zero(position.quantity)
            invested = decimal_or_zero(position.invested_czk)
            position.market_value_czk = quantity * price * rate
            position.profit_czk = position.market_value_czk - invested
            position.profit_pct = (position.profit_czk / invested) if invested else None

        if previous_price:
            change_pct = (price - previous_price) / previous_price * 100
            if abs(change_pct) >= Decimal(str(threshold)):
                movers.append(
                    {
                        "ticker": ticker,
                        "previous_price": to_number(previous_price),
                        "current_price": to_number(price),
                        "change_pct": to_number(change_pct),
                    }
                )

        transactions = db.scalars(select(StockTransaction).where(StockTransaction.ticker == ticker)).all()
        for transaction in transactions:
            if (transaction.movement_type or "").lower() in {"dividenda", "prodej"}:
                continue
            transaction.current_price = price
            amount_czk = decimal_or_zero(transaction.amount_czk)
            current_value = decimal_or_zero(transaction.quantity) * price * rate
            transaction.difference_czk = current_value - amount_czk
            transaction.difference_pct = transaction.difference_czk / amount_czk if amount_czk else None
        watched_rows = db.scalars(select(WatchlistStock).where(WatchlistStock.ticker == ticker)).all()
        for watched in watched_rows:
            watched.current_price = price
            if rate_currency:
                watched.currency = rate_currency
            limit_price = decimal_or_zero(watched.limit_price)
            watched.difference_pct = (price - limit_price) / price if price else None
        updated += 1
    db.commit()
    return {"updated": updated, "errors": errors[:25], "movers": sorted(movers, key=lambda m: abs(m["change_pct"]), reverse=True)}


def compute_alerts(db: Session, threshold_pct: Decimal | float = Decimal("10")) -> dict[str, Any]:
    """Pragmatic replacement for the watchlist/drawdown alerts computed by
    AktualizujHodnotu.bas / AkcieStatistika.bas: tickers whose price has reached
    the watchlist limit, and portfolio positions down more than ``threshold_pct``
    versus their average purchase cost. Day-over-day price-move alerts are
    reported by `refresh_current_prices` (its `movers` list), since that is the
    point where the previous price is still known.
    """
    threshold = Decimal(str(threshold_pct))
    watchlist_alerts: list[dict[str, Any]] = []
    for row in db.scalars(select(WatchlistStock).where(WatchlistStock.ticker.is_not(None))).all():
        if row.current_price is None or row.limit_price is None:
            continue
        if row.current_price <= row.limit_price:
            watchlist_alerts.append(
                {
                    "ticker": row.ticker,
                    "name": row.name,
                    "current_price": to_number(row.current_price),
                    "limit_price": to_number(row.limit_price),
                    "currency": row.currency,
                }
            )

    drawdown_alerts: list[dict[str, Any]] = []
    for row in db.scalars(select(PortfolioPosition)).all():
        if row.profit_pct is None:
            continue
        if row.profit_pct <= -(threshold / 100):
            drawdown_alerts.append(
                {
                    "ticker": row.ticker,
                    "name": row.name,
                    "profit_pct": to_number(row.profit_pct),
                    "profit_czk": to_number(row.profit_czk),
                    "market_value_czk": to_number(row.market_value_czk),
                }
            )

    return {
        "threshold_pct": to_number(threshold),
        "watchlist_limit_breaches": sorted(watchlist_alerts, key=lambda item: item["ticker"] or ""),
        "portfolio_drawdowns": sorted(drawdown_alerts, key=lambda item: item["profit_pct"]),
    }


def movement_is_buy(value: str | None) -> bool:
    # AkcieStatistika.bas is explicit about this: "POUZE nakup a prodej -- vsechny
    # ostatni pohyby (Tip apod.) se preskakuji" - watchlist/tip/plan rows are
    # hypothetical, not real purchases, and must not feed the real portfolio or
    # daily-statistics totals (that previously made recalculated figures diverge
    # from the imported Excel numbers).
    text = (value or "").strip().lower()
    return text in {"nákup", "nakup", "buy"}


def movement_is_sell(value: str | None) -> bool:
    text = (value or "").strip().lower()
    return text in {"prodej", "sell"}


def movement_is_dividend(value: str | None) -> bool:
    text = (value or "").strip().lower()
    return text in {"dividenda", "dividend"}


def recalculate_stocks(
    db: Session, dry_run: bool = False, date_from: date | None = None, threshold_pct: Decimal | float = Decimal("10")
) -> dict[str, Any]:
    """Recompute portfolio positions and daily statistics from `stock_transactions`.

    Portfolio positions are always a full recompute (they reflect current state,
    not a point in time). Daily statistics mirror the incremental behaviour of
    `AktualizujStatistiku` in AkcieStatistika.bas ("Zpracovat od"): cumulative
    running totals (Σ EUR/USD/CZK, invested, dividends) are always accumulated
    from the very first transaction so the figures stay correct, but when
    `date_from` is given, only statistic rows on or after that date are
    (re)written - earlier rows already stored in `daily_statistics` are left
    untouched. Omit `date_from` for a full recompute of the whole history.

    The "Hod. EUR/USD/CZK" / "Celk. Hod. CZK" / "Nereal. zisk" columns are the
    actual market value of the shares held on each day - quantity held that day
    times the historical closing price on/before that day (fetched from Yahoo
    Finance per ticker), exactly like AkcieStatistika.bas's GetPriceAtOrBefore.
    They are NOT the invested amount merely re-expressed in CZK (that used to
    be the case here, which meant "Nereal. zisk" only ever reflected FX-rate
    drift, never a real stock-price gain or loss).
    """
    existing_prices = {
        row.ticker: row.current_price for row in db.scalars(select(PortfolioPosition)).all() if row.ticker and row.current_price is not None
    }
    transactions = db.scalars(select(StockTransaction).order_by(StockTransaction.traded_on.nullslast(), StockTransaction.id)).all()

    positions: dict[str, dict[str, Any]] = {}
    buy_dates: set[date] = set()
    daily_buys: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    daily_invested: dict[date, Decimal] = defaultdict(Decimal)
    daily_dividends: dict[date, Decimal] = defaultdict(Decimal)
    # Signed quantity change per ticker per day - needed to reconstruct how many
    # shares were actually held on any given historical day, so they can be
    # valued at that day's price.
    ticker_qty_events: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    for transaction in transactions:
        ticker = normalize_ticker(transaction.ticker, transaction.market)
        movement = transaction.movement_type
        traded_on = transaction.traded_on
        quantity = decimal_or_zero(transaction.quantity)
        amount_czk = decimal_or_zero(transaction.amount_czk)
        gross = decimal_or_zero(transaction.gross_amount_ccy)
        currency = (transaction.currency or "CZK").upper()

        if movement_is_dividend(movement):
            if traded_on:
                daily_dividends[traded_on] += amount_czk
            continue
        if not ticker:
            continue

        position = positions.setdefault(
            ticker,
            {
                "ticker": ticker,
                "name": transaction.instrument_name,
                "quantity": ZERO,
                "invested_czk": ZERO,
                # Only ever set from an actual "nákup" row below - a watchlist/tip/plan
                # row touching this ticker first (chronologically) must not masquerade
                # as the real first purchase date.
                "first_buy_date": None,
                "currency": currency,
                "current_price": existing_prices.get(ticker) or transaction.current_price,
            },
        )
        if transaction.current_price is not None:
            position["current_price"] = transaction.current_price
        position["currency"] = currency or position["currency"]
        if transaction.instrument_name:
            position["name"] = transaction.instrument_name

        if movement_is_buy(movement):
            position["quantity"] += quantity
            position["invested_czk"] += amount_czk
            if traded_on:
                buy_dates.add(traded_on)
                daily_buys[traded_on][currency] += gross
                daily_invested[traded_on] += amount_czk
                ticker_qty_events[ticker][traded_on] += quantity
                if position["first_buy_date"] is None or traded_on < position["first_buy_date"]:
                    position["first_buy_date"] = traded_on
        elif movement_is_sell(movement):
            old_quantity = position["quantity"]
            if old_quantity:
                average_cost = position["invested_czk"] / old_quantity
                position["invested_czk"] -= abs(quantity) * average_cost
            position["quantity"] += quantity
            if traded_on:
                ticker_qty_events[ticker][traded_on] += quantity

    computed_positions: list[PortfolioPosition] = []
    total_market_value = ZERO
    for data in positions.values():
        quantity = decimal_or_zero(data["quantity"])
        if quantity <= ZERO:
            continue
        price = decimal_or_zero(data["current_price"])
        rate = latest_rate(db, data["currency"])
        market_value = quantity * price * rate
        invested = decimal_or_zero(data["invested_czk"])
        profit = market_value - invested
        total_market_value += market_value
        computed_positions.append(
            PortfolioPosition(
                ticker=data["ticker"],
                name=data["name"],
                quantity=quantity,
                current_price=as_decimal(price),
                currency=data["currency"],
                market_value_czk=market_value,
                invested_czk=invested,
                profit_czk=profit,
                profit_pct=(profit / invested) if invested else None,
                first_buy_date=data["first_buy_date"],
            )
        )

    for position in computed_positions:
        position.portfolio_share_pct = (
            decimal_or_zero(position.market_value_czk) / total_market_value if total_market_value else None
        )

    alert_threshold = Decimal(str(threshold_pct))
    computed_stats: list[DailyStatistic] = []
    total_eur = ZERO
    total_usd = ZERO
    total_czk = ZERO
    invested_total = ZERO
    dividends_total = ZERO
    stat_dates = sorted(buy_dates | set(daily_dividends))
    if stat_dates:
        start_date = stat_dates[0]
        end_date = max(date.today(), stat_dates[-1])
        # Only working days (Po-Pa) are filled in - AkcieStatistika.bas never
        # generates weekend rows ("Pridat vsechny pracovni dny (Po-Pa)"), no
        # trading happens then anyway. A real transaction/dividend that did
        # land on a weekend still gets its row, exactly like the VBA (it never
        # filters out actual data, only the synthetic empty fill days).
        calendar_dates = [
            candidate
            for offset in range((end_date - start_date).days + 1)
            for candidate in [start_date + timedelta(days=offset)]
            if candidate.weekday() < 5 or candidate in buy_dates or candidate in daily_dividends
        ]
    else:
        calendar_dates = []

    # Historical closing prices per ticker, fetched once for the whole range -
    # mirrors AkcieStatistika.bas's "Krok 5b" Yahoo Finance step.
    price_dates: dict[str, list[date]] = {}
    price_values: dict[str, list[Decimal]] = {}
    if calendar_dates:
        for ticker in positions:
            history = fetch_yahoo_history(ticker, start_date, date.today())
            points = sorted(history.get("points") or [])
            price_dates[ticker] = [point[0] for point in points]
            price_values[ticker] = [point[1] for point in points]

    def price_at_or_before(ticker: str, target: date) -> Decimal | None:
        dates = price_dates.get(ticker)
        if not dates:
            return None
        idx = bisect.bisect_right(dates, target) - 1
        if idx < 0:
            return None
        return price_values[ticker][idx]

    ticker_running_qty: dict[str, Decimal] = defaultdict(Decimal)
    previous_total_value_czk = ZERO
    previous_invested_total = ZERO
    for stat_date in calendar_dates:
        bought_eur = daily_buys[stat_date]["EUR"]
        bought_usd = daily_buys[stat_date]["USD"]
        bought_czk = daily_buys[stat_date]["CZK"] or daily_invested[stat_date] if daily_buys[stat_date]["CZK"] else ZERO
        total_eur += bought_eur
        total_usd += bought_usd
        total_czk += bought_czk
        invested_total += daily_invested[stat_date]
        dividends_total += daily_dividends[stat_date]
        eur_rate = rate_for_day(db, "EUR", stat_date)
        usd_rate = rate_for_day(db, "USD", stat_date)
        eur_in_czk = total_eur * eur_rate
        usd_in_czk = total_usd * usd_rate

        for ticker, events in ticker_qty_events.items():
            change = events.get(stat_date)
            if change:
                ticker_running_qty[ticker] += change

        # Market value of everything actually held on this day, bucketed by
        # trading currency - not the invested/purchased amount. Also flags two
        # kinds of "Upozorneni" (AkcieStatistika.bas): a day-over-day price
        # move past `alert_threshold`, or a ticker held that day with no price
        # data at all for it.
        value_eur = ZERO
        value_usd = ZERO
        value_czk = ZERO
        movers: list[str] = []
        missing_price_tickers: list[str] = []
        for ticker, qty in ticker_running_qty.items():
            if qty <= ZERO:
                continue
            price = price_at_or_before(ticker, stat_date)
            if price is None:
                missing_price_tickers.append(ticker)
                continue
            ticker_currency = (positions[ticker]["currency"] or "CZK").upper()
            holding_value = qty * price
            if ticker_currency == "EUR":
                value_eur += holding_value
            elif ticker_currency == "USD":
                value_usd += holding_value
            else:
                value_czk += holding_value

            price_yesterday = price_at_or_before(ticker, stat_date - timedelta(days=1))
            if price_yesterday and price_yesterday > ZERO:
                pct_change = (price / price_yesterday - ONE) * Decimal("100")
                if abs(pct_change) >= alert_threshold:
                    movers.append(f"{ticker} (D:{pct_change:+.1f}%)")

        alert_bits = []
        if movers:
            alert_bits.append(", ".join(movers))
        if missing_price_tickers:
            alert_bits.append("Chybí cena: " + ", ".join(missing_price_tickers))
        day_alerts = " | ".join(alert_bits) if alert_bits else None

        total_value_czk = value_czk + (value_eur * eur_rate) + (value_usd * usd_rate)
        unrealized = total_value_czk - invested_total
        # Daily P&L excludes money newly invested that day - only the change in
        # market value of what was already held, matching AkcieStatistika.bas.
        daily_profit = (total_value_czk - previous_total_value_czk) - (invested_total - previous_invested_total)
        previous_total_value_czk = total_value_czk
        previous_invested_total = invested_total
        if date_from is not None and stat_date < date_from:
            continue
        computed_stats.append(
            DailyStatistic(
                stat_date=stat_date,
                bought_eur=bought_eur,
                total_eur=total_eur,
                eur_in_czk=eur_in_czk,
                value_eur=value_eur,
                eur_rate=eur_rate,
                bought_usd=bought_usd,
                total_usd=total_usd,
                usd_in_czk=usd_in_czk,
                value_usd=value_usd,
                usd_rate=usd_rate,
                bought_czk=bought_czk,
                total_czk=total_czk,
                value_czk=value_czk,
                invested_czk=invested_total,
                total_value_czk=total_value_czk,
                unrealized_profit_czk=unrealized,
                dividends=daily_dividends[stat_date],
                dividends_total=dividends_total,
                profit_pct=(
                    ((total_value_czk + dividends_total - invested_total) / invested_total) if invested_total else None
                ),
                daily_profit_czk=daily_profit,
                alerts=day_alerts,
            )
        )

    if not dry_run:
        db.query(PortfolioPosition).delete()
        for position in computed_positions:
            db.add(position)
        if date_from is not None:
            db.query(DailyStatistic).filter(DailyStatistic.stat_date >= date_from).delete()
        else:
            db.query(DailyStatistic).delete()
        for statistic in computed_stats:
            db.add(statistic)
        db.commit()

    return {
        "dry_run": dry_run,
        "date_from": date_from,
        "transactions": len(transactions),
        "portfolio_positions": len(computed_positions),
        "daily_statistics": len(computed_stats),
    }


def fetch_yahoo_history(ticker: str, date_from: date, date_to: date) -> dict[str, Any]:
    """Daily close prices for a ticker between two dates, via Yahoo's chart API
    (same endpoint family as `fetch_yahoo_price`, with an explicit range)."""
    period1 = int(datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    period2 = int(datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    symbol = quote(ticker)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={period1}&period2={period2}&interval=1d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?period1={period1}&period2={period2}&interval=1d",
    ]
    headers = {"User-Agent": "FinanceSEMA/1.0"}
    for url in urls:
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            result = payload["chart"]["result"][0]
            timestamps = result.get("timestamp") or []
            closes = result["indicators"]["quote"][0].get("close") or []
            currency = result.get("meta", {}).get("currency")
            points: list[tuple[date, Decimal]] = []
            for ts, close in zip(timestamps, closes):
                if close is None:
                    continue
                trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                points.append((trade_date, decimal_or_zero(close)))
            if points:
                return {"currency": currency, "points": points}
        except Exception:
            continue
    return {"currency": None, "points": []}


def build_ticker_history(db: Session, ticker: str, date_from: date, date_to: date) -> dict[str, Any]:
    """Port of TickerHistory.bas: daily price history for a ticker combined with
    the cumulative purchases from `stock_transactions`, showing quantity held,
    market value and profit/loss in both the trading currency (CCY) and CZK for
    every trading day in the requested range.
    """
    normalized = normalize_ticker(ticker) or ticker.strip().upper()
    history = fetch_yahoo_history(normalized, date_from, date_to)
    points = [p for p in history["points"] if date_from <= p[0] <= date_to]
    if not points:
        return {"ticker": normalized, "currency": history.get("currency"), "rows": [], "summary": None}
    currency = (history.get("currency") or "CZK").upper()

    buy_transactions = db.scalars(
        select(StockTransaction)
        .where(StockTransaction.ticker == normalized, StockTransaction.traded_on.is_not(None))
        .order_by(StockTransaction.traded_on)
    ).all()
    daily_buy_qty: dict[date, Decimal] = defaultdict(Decimal)
    daily_buy_cm: dict[date, Decimal] = defaultdict(Decimal)
    daily_buy_czk: dict[date, Decimal] = defaultdict(Decimal)
    for transaction in buy_transactions:
        if not movement_is_buy(transaction.movement_type):
            continue
        traded_on = transaction.traded_on
        daily_buy_qty[traded_on] += decimal_or_zero(transaction.quantity)
        daily_buy_cm[traded_on] += decimal_or_zero(transaction.gross_amount_ccy)
        daily_buy_czk[traded_on] += decimal_or_zero(transaction.amount_czk)

    rows: list[dict[str, Any]] = []
    cum_qty = ZERO
    cum_cost_cm = ZERO
    cum_cost_czk = ZERO
    previous_price_cm: Decimal | None = None
    previous_price_czk: Decimal | None = None
    for trade_date, price_cm in sorted(points):
        rate = rate_for_day(db, currency, trade_date)
        price_czk = price_cm * rate
        qty_before_today = cum_qty
        buy_qty = daily_buy_qty.get(trade_date, ZERO)
        buy_cm = daily_buy_cm.get(trade_date, ZERO)
        buy_czk = daily_buy_czk.get(trade_date, ZERO)
        cum_qty += buy_qty
        cum_cost_cm += buy_cm
        cum_cost_czk += buy_czk
        value_cm = cum_qty * price_cm
        value_czk = cum_qty * price_czk
        change_pct = ((price_cm - previous_price_cm) / previous_price_cm * 100) if previous_price_cm else None
        change_value_cm = qty_before_today * (price_cm - previous_price_cm) if previous_price_cm is not None else None
        change_value_czk = (
            qty_before_today * (price_czk - previous_price_czk) if previous_price_czk is not None else None
        )
        rows.append(
            {
                "trade_date": trade_date,
                "price_ccy": to_number(price_cm),
                "rate": to_number(rate),
                "price_czk": to_number(price_czk),
                "change_pct": to_number(change_pct) if change_pct is not None else None,
                "buy_quantity": to_number(buy_qty) if buy_qty else None,
                "buy_ccy": to_number(buy_cm) if buy_cm else None,
                "buy_czk": to_number(buy_czk) if buy_czk else None,
                "cumulative_quantity": to_number(cum_qty),
                "value_ccy": to_number(value_cm),
                "value_czk": to_number(value_czk),
                "change_value_ccy": to_number(change_value_cm) if change_value_cm is not None else None,
                "change_value_czk": to_number(change_value_czk) if change_value_czk is not None else None,
                "profit_ccy": to_number(value_cm - cum_cost_cm),
                "profit_czk": to_number(value_czk - cum_cost_czk),
            }
        )
        previous_price_cm = price_cm
        previous_price_czk = price_czk

    last = rows[-1]
    summary = {
        "cumulative_quantity": last["cumulative_quantity"],
        "value_ccy": last["value_ccy"],
        "value_czk": last["value_czk"],
        "profit_ccy": last["profit_ccy"],
        "profit_czk": last["profit_czk"],
        "invested_ccy": to_number(cum_cost_cm),
        "invested_czk": to_number(cum_cost_czk),
    }

    return {"ticker": normalized, "currency": currency, "rows": rows, "summary": summary}
