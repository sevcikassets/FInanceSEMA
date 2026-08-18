from __future__ import annotations

import bisect
import json
import re
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
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
    # Patria's own web export copy-pastes each column as two tab-separated
    # cells (a value cell plus an empty spacer cell - a rendering quirk of
    # its table, not a real extra column). Collapsing repeated tabs to one
    # before splitting handles that transparently; a plain single-tab paste
    # (e.g. hand-built TSV, or the older format this parser originally
    # targeted) is unaffected since it has no repeated tabs to collapse.
    # The export also inserts a horizontal-rule line of underscores between
    # each trade's two-line block - not blank, so a naive blank-line filter
    # leaves it in, throwing off the "always process two lines at a time"
    # pairing below and silently dropping every other trade from that point
    # on (confirmed against a real 5-trade paste that only produced 3 rows).
    lines = [
        re.sub(r"\t+", "\t", line) for line in text.splitlines() if line.strip() and line.strip().strip("_")
    ]
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

        if quantity == ZERO:
            # A real trade always moves a nonzero number of shares - zero here
            # means the two lines didn't actually line up with the expected
            # columns (e.g. an unrecognised export format), not a valid trade.
            continue

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


# Mirrors StockTransaction's own column limits - a malformed paste (unexpected
# column layout, e.g. an ISIN landing in the currency field) must be reported
# and skipped, not crash the whole batch on insert like a StringDataRightTruncation
# from Postgres would (which the browser then only ever sees as an opaque
# "network error", since the unhandled 500 has no CORS header on it).
PATRIA_FIELD_LIMITS = {"instrument_name": 255, "isin": 32, "market": 32, "currency": 8}


def import_patria_trades(db: Session, portfolio_id: uuid.UUID, text: str) -> dict[str, Any]:
    trades = parse_patria_text(text)
    inserted = 0
    skipped = 0
    rejected: list[dict[str, str]] = []
    for trade in trades:
        oversized = [
            field
            for field, limit in PATRIA_FIELD_LIMITS.items()
            if getattr(trade, field) and len(getattr(trade, field)) > limit
        ]
        if oversized:
            rejected.append(
                {
                    "traded_on": trade.traded_on.isoformat(),
                    "instrument_name": trade.instrument_name or "?",
                    "reason": f"Řádek neodpovídá očekávanému formátu exportu (sloupec {', '.join(oversized)})",
                }
            )
            continue
        ticker = ticker_from_existing_data(db, trade.isin, trade.instrument_name, trade.market)
        # Patria's own export only ever gives the plain security name ("SPDR
        # USA S/C VALUE") - every other import path (the historical Excel
        # sheet) stores it as "TICKER - Name" ("ZPRV - SPDR USA S/C VALUE"),
        # which is what the rest of the app displays. Match that convention
        # once a ticker is actually resolved, using the bare symbol (before
        # any Yahoo exchange suffix like ".DE") to match how it reads
        # elsewhere; leave the plain name alone if no ticker could be found.
        # Done before the duplicate check below so that check compares
        # against what's actually stored, not the pre-import raw name.
        instrument_name = trade.instrument_name
        if ticker and instrument_name:
            display_ticker = ticker.split(".")[0]
            instrument_name = f"{display_ticker} - {instrument_name}"
        duplicate = db.scalar(
            select(StockTransaction)
            .where(
                StockTransaction.portfolio_id == portfolio_id,
                StockTransaction.traded_on == trade.traded_on,
                StockTransaction.instrument_name == instrument_name,
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
        db.add(
            StockTransaction(
                portfolio_id=portfolio_id,
                traded_on=trade.traded_on,
                instrument_type="Akcie",
                movement_type=trade.movement_type,
                instrument_name=instrument_name,
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
    return {
        "parsed": len(trades),
        "inserted": inserted,
        "skipped_duplicates": skipped,
        "rejected": rejected,
    }


# Yahoo's chart endpoint blocks requests whose User-Agent doesn't look like a
# real browser (a generic UA like "FinanceSEMA/1.0" gets silently refused with
# a 403/999 on some networks) - mirrors an actual Chrome UA to stay reliable.
YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def _yahoo_get(url: str, timeout: int = 8, attempts: int = 2) -> dict[str, Any] | None:
    """GET a Yahoo chart URL, with one short retry - Yahoo occasionally throttles
    back-to-back requests (recalculating a portfolio with many tickers fires
    dozens of these in a row), and a bare single attempt turns a transient
    throttle into a permanently-missing price for that ticker/day.

    Timeout is deliberately short (default 8s, was 12-15s) and the retry
    backoff brief (0.2s): a recalculation fetches history for every ticker in
    the portfolio, one request per ticker - a slow-but-not-quite-failing
    Yahoo response used to be able to stack up to minutes of latency across
    ~80+ tickers and make the browser's fetch() itself time out ("Failed to
    fetch"), even though the recalculation was still running server-side.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers=YAHOO_HEADERS)
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - network is inherently flaky here
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.2)
    if last_error is not None:
        raise last_error
    return None


def _extract_quote(payload: dict[str, Any]) -> tuple[Any, str | None]:
    """Pull (price, currency) out of either Yahoo response shape: the /v8/chart
    endpoint (chart.result[0].meta) or the /v7/quote endpoint
    (quoteResponse.result[0]) - GetStooqPrice in AktualizujHodnotu.bas tries
    both, since one occasionally answers when the other is throttled."""
    chart_results = (payload.get("chart") or {}).get("result") or []
    if chart_results:
        meta = chart_results[0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        if price is not None:
            return price, meta.get("currency")
    quote_results = (payload.get("quoteResponse") or {}).get("result") or []
    if quote_results:
        entry = quote_results[0]
        price = entry.get("regularMarketPrice") or entry.get("regularMarketPreviousClose")
        if price is not None:
            return price, entry.get("currency")
    return None, None


def fetch_yahoo_price(ticker: str) -> dict[str, Decimal | str | None]:
    # Same four endpoint variants as AktualizujHodnotu.bas's GetStooqPrice (a
    # misleading name - it is Yahoo, not Stooq): two chart-endpoint hosts plus
    # two quote-endpoint hosts, tried in order until one answers with a price.
    symbol = quote(ticker)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
        f"https://query2.finance.yahoo.com/v7/finance/quote?symbols={symbol}",
        f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}",
    ]
    for url in urls:
        try:
            payload = _yahoo_get(url)
            price, currency = _extract_quote(payload)
            if price is not None:
                return {"price": decimal_or_zero(price), "currency": currency}
        except Exception:
            continue
    return {"price": None, "currency": None}


def refresh_current_prices(
    db: Session, portfolio_id: uuid.UUID, threshold_pct: Decimal | float = Decimal("10")
) -> dict[str, Any]:
    threshold = float(threshold_pct)
    positions = db.scalars(
        select(PortfolioPosition).where(PortfolioPosition.portfolio_id == portfolio_id, PortfolioPosition.ticker.is_not(None))
    ).all()
    watchlist = db.scalars(
        select(WatchlistStock).where(WatchlistStock.portfolio_id == portfolio_id, WatchlistStock.ticker.is_not(None))
    ).all()
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
        position = db.get(PortfolioPosition, (portfolio_id, ticker))
        previous_price = position.current_price if position is not None else None
        rate_currency = str(quote_data["currency"]).upper() if quote_data.get("currency") else None
        currency_for_rate = rate_currency or (position.currency if position is not None else None)
        if not currency_for_rate:
            latest_transaction = db.scalar(
                select(StockTransaction)
                .where(
                    StockTransaction.portfolio_id == portfolio_id,
                    StockTransaction.ticker == ticker,
                    StockTransaction.currency.is_not(None),
                )
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

        transactions = db.scalars(
            select(StockTransaction).where(StockTransaction.portfolio_id == portfolio_id, StockTransaction.ticker == ticker)
        ).all()
        for transaction in transactions:
            if (transaction.movement_type or "").lower() in {"dividenda", "prodej"}:
                continue
            transaction.current_price = price
            amount_czk = decimal_or_zero(transaction.amount_czk)
            current_value = decimal_or_zero(transaction.quantity) * price * rate
            transaction.difference_czk = current_value - amount_czk
            transaction.difference_pct = transaction.difference_czk / amount_czk if amount_czk else None
        watched_rows = db.scalars(
            select(WatchlistStock).where(WatchlistStock.portfolio_id == portfolio_id, WatchlistStock.ticker == ticker)
        ).all()
        for watched in watched_rows:
            watched.current_price = price
            if rate_currency:
                watched.currency = rate_currency
            limit_price = decimal_or_zero(watched.limit_price)
            watched.difference_pct = (price - limit_price) / price if price else None
        updated += 1
    db.commit()
    return {"updated": updated, "errors": errors[:25], "movers": sorted(movers, key=lambda m: abs(m["change_pct"]), reverse=True)}


DAILY_MOVER_RE = re.compile(r"(\S+) \(D:([+-][\d.]+)%\)")


def compute_alerts(db: Session, portfolio_id: uuid.UUID, threshold_pct: Decimal | float = Decimal("10")) -> dict[str, Any]:
    """The three "Upozorneni" categories AkcieStatistika.bas/AktualizujHodnotu.bas
    surface: tickers whose price has reached the watchlist limit, portfolio
    positions down more than ``threshold_pct`` versus their average purchase
    cost, and day-over-day price moves past the same threshold. The first two
    are computed live from current prices; day movers are parsed out of the
    most recent DailyStatistic.alerts text (already computed properly from
    Yahoo historical closes during a recalculation - see recalculate_stocks),
    rather than recomputed here from only the last-refreshed price.
    """
    threshold = Decimal(str(threshold_pct))
    watchlist_alerts: list[dict[str, Any]] = []
    for row in db.scalars(
        select(WatchlistStock).where(WatchlistStock.portfolio_id == portfolio_id, WatchlistStock.ticker.is_not(None))
    ).all():
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
    for row in db.scalars(select(PortfolioPosition).where(PortfolioPosition.portfolio_id == portfolio_id)).all():
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

    latest_stat = db.scalar(
        select(DailyStatistic)
        .where(DailyStatistic.portfolio_id == portfolio_id)
        .order_by(desc(DailyStatistic.stat_date))
        .limit(1)
    )
    daily_movers: list[dict[str, Any]] = []
    if latest_stat is not None and latest_stat.alerts:
        for ticker, pct_text in DAILY_MOVER_RE.findall(latest_stat.alerts):
            daily_movers.append({"ticker": ticker, "change_pct": float(pct_text)})
    daily_movers.sort(key=lambda item: abs(item["change_pct"]), reverse=True)

    return {
        "threshold_pct": to_number(threshold),
        "watchlist_limit_breaches": sorted(watchlist_alerts, key=lambda item: item["ticker"] or ""),
        "portfolio_drawdowns": sorted(drawdown_alerts, key=lambda item: item["profit_pct"]),
        "daily_movers": daily_movers,
        "daily_movers_as_of": latest_stat.stat_date.isoformat() if latest_stat is not None else None,
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
    db: Session,
    portfolio_id: uuid.UUID,
    dry_run: bool = False,
    date_from: date | None = None,
    threshold_pct: Decimal | float = Decimal("10"),
    drop_threshold_pct: Decimal | float = Decimal("10"),
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

    The "alerts" text column is rebuilt for the full history every run,
    matching the original Excel workbook's per-day "Info" column exactly:
    a position down more than `drop_threshold_pct` versus its own cost
    basis as of that specific day ("T:..."), a watched ticker at or below
    its (current) limit price on that day ("L:..."), and a day-over-day
    price move past `threshold_pct` ("D:..."), combined.
    """
    existing_prices = {
        row.ticker: row.current_price
        for row in db.scalars(select(PortfolioPosition).where(PortfolioPosition.portfolio_id == portfolio_id)).all()
        if row.ticker and row.current_price is not None
    }
    transactions = db.scalars(
        select(StockTransaction)
        .where(StockTransaction.portfolio_id == portfolio_id)
        .order_by(StockTransaction.traded_on.nullslast(), StockTransaction.id)
    ).all()
    # Every watched ticker with a limit price - needed for the historical "L:"
    # (watchlist-limit) alert below, including tickers that are only watched
    # and never actually bought (so absent from `positions`/`transactions`).
    watchlist_rows = [
        row
        for row in db.scalars(
            select(WatchlistStock).where(WatchlistStock.portfolio_id == portfolio_id, WatchlistStock.ticker.is_not(None))
        ).all()
        if row.limit_price is not None
    ]

    positions: dict[str, dict[str, Any]] = {}
    buy_dates: set[date] = set()
    daily_buys: dict[date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    daily_invested: dict[date, Decimal] = defaultdict(Decimal)
    daily_dividends: dict[date, Decimal] = defaultdict(Decimal)
    # Realized gain/loss booked at the moment of each "sell" - proceeds minus
    # the average-cost basis removed (see the sell branch below). Same daily-
    # flow shape as daily_dividends; unlike unrealized profit this is exact,
    # not mark-to-market, so no historical price lookups are needed for it.
    daily_realized_profit: dict[date, Decimal] = defaultdict(Decimal)
    # Signed quantity change per ticker per day - needed to reconstruct how many
    # shares were actually held on any given historical day, so they can be
    # valued at that day's price.
    ticker_qty_events: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    # Signed invested_czk (cost basis) change per ticker per day, mirroring
    # ticker_qty_events - lets the day loop reconstruct not just how many
    # shares were held on a given historical day, but what they cost, so a
    # historical "T:" (drawdown) alert can be computed for that exact day
    # instead of only ever reflecting today's final cost basis.
    ticker_invested_events: dict[str, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

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
                ticker_invested_events[ticker][traded_on] += amount_czk
                if position["first_buy_date"] is None or traded_on < position["first_buy_date"]:
                    position["first_buy_date"] = traded_on
        elif movement_is_sell(movement):
            old_quantity = position["quantity"]
            if old_quantity:
                average_cost = position["invested_czk"] / old_quantity
                cost_basis_removed = abs(quantity) * average_cost
                position["invested_czk"] -= cost_basis_removed
                if traded_on:
                    daily_realized_profit[traded_on] += amount_czk - cost_basis_removed
                    ticker_invested_events[ticker][traded_on] -= cost_basis_removed
            position["quantity"] += quantity
            if traded_on:
                ticker_qty_events[ticker][traded_on] += quantity

    # Quantity/invested basis only for now - priced further down once the fresh
    # Yahoo history (including today's live-quote patch) has been fetched, so
    # positions are valued with the exact same price DailyStatistic uses for
    # "today" instead of whatever stale price was last stored (previously this
    # only updated via the separate "Ceny" refresh action, so "Zisk portfolia"
    # could silently drift out of sync with the recalculated daily statistics).
    computed_positions: list[PortfolioPosition] = []
    for data in positions.values():
        quantity = decimal_or_zero(data["quantity"])
        if quantity <= ZERO:
            continue
        computed_positions.append(
            PortfolioPosition(
                portfolio_id=portfolio_id,
                ticker=data["ticker"],
                name=data["name"],
                quantity=quantity,
                currency=data["currency"],
                invested_czk=decimal_or_zero(data["invested_czk"]),
                first_buy_date=data["first_buy_date"],
            )
        )

    alert_threshold = Decimal(str(threshold_pct))
    drop_threshold = Decimal(str(drop_threshold_pct))
    computed_stats: list[DailyStatistic] = []
    total_eur = ZERO
    total_usd = ZERO
    total_czk = ZERO
    invested_total = ZERO
    dividends_total = ZERO
    realized_profit_total = ZERO
    stat_dates = sorted(buy_dates | set(daily_dividends) | set(daily_realized_profit))
    if stat_dates:
        start_date = stat_dates[0]
        end_date = max(date.today(), stat_dates[-1])
        # Only working days (Po-Pa) are filled in - AkcieStatistika.bas never
        # generates weekend rows ("Pridat vsechny pracovni dny (Po-Pa)"), no
        # trading happens then anyway. A real transaction/dividend/sell that
        # did land on a weekend still gets its row, exactly like the VBA (it
        # never filters out actual data, only the synthetic empty fill days).
        calendar_dates = [
            candidate
            for offset in range((end_date - start_date).days + 1)
            for candidate in [start_date + timedelta(days=offset)]
            if candidate.weekday() < 5
            or candidate in buy_dates
            or candidate in daily_dividends
            or candidate in daily_realized_profit
        ]
    else:
        calendar_dates = []

    # Historical closing prices per ticker, fetched once for the whole range -
    # mirrors AkcieStatistika.bas's "Krok 5b" Yahoo Finance step.
    price_dates: dict[str, list[date]] = {}
    price_values: dict[str, list[Decimal]] = {}
    # Tickers for which Yahoo returned no price history at all (network problem,
    # rate limiting, or an unrecognised symbol) - tracked separately from the
    # per-day "Chybí cena" alerts so a full outage is visible as one clear
    # number instead of being buried inside dozens of daily alert strings.
    tickers_without_price_history: list[str] = []
    # Fetched concurrently, not one ticker at a time: a portfolio with ~80+
    # distinct tickers used to fire that many sequential blocking HTTP calls,
    # which - especially once retries were added for reliability - could take
    # minutes and made the browser's own fetch() time out ("Failed to fetch")
    # well before the recalculation itself finished. A small bounded pool
    # keeps the same request pattern per ticker but runs them side by side.
    YAHOO_CONCURRENCY = 8
    # Watchlist-only tickers (watched but never actually bought) need their own
    # historical price series too, purely for the "L:" alert below - they'd
    # otherwise never get a Yahoo history fetch at all (positions is empty for
    # them), so price_at_or_before would silently return None for every day.
    watchlist_tickers = {normalize_ticker(row.ticker, None) for row in watchlist_rows if row.ticker}
    if calendar_dates:
        ticker_list = sorted(set(positions) | watchlist_tickers)
        with ThreadPoolExecutor(max_workers=YAHOO_CONCURRENCY) as pool:
            histories = pool.map(lambda t: fetch_yahoo_history(t, start_date, date.today()), ticker_list)
        for ticker, history in zip(ticker_list, histories):
            points = sorted(history.get("points") or [])
            price_dates[ticker] = [point[0] for point in points]
            price_values[ticker] = [point[1] for point in points]
            if not points:
                tickers_without_price_history.append(ticker)

        # "Krok 5b-2" in AkcieStatistika.bas: the daily-candle history endpoint
        # can lack an exact close for the latest trading day (published with a
        # delay, or the macro/recalc runs mid-session) - Excel then patches that
        # one day in with a live quote instead of silently falling back to an
        # older price. Only bother for tickers still actually held, to avoid
        # doubling the number of Yahoo requests for positions long since sold.
        latest_stat_date = date.today()
        while latest_stat_date.weekday() >= 5:
            latest_stat_date -= timedelta(days=1)
        still_held = [
            ticker
            for ticker, events in ticker_qty_events.items()
            if sum(events.values()) > ZERO
            and not (price_dates.get(ticker) and price_dates[ticker][-1] == latest_stat_date)
        ]
        if still_held:
            with ThreadPoolExecutor(max_workers=YAHOO_CONCURRENCY) as pool:
                live_quotes = pool.map(fetch_yahoo_price, still_held)
            for ticker, quote_data in zip(still_held, live_quotes):
                price = quote_data.get("price")
                if not isinstance(price, Decimal) or price <= ZERO:
                    continue
                dates = price_dates.setdefault(ticker, [])
                values = price_values.setdefault(ticker, [])
                idx = bisect.bisect_left(dates, latest_stat_date)
                if idx < len(dates) and dates[idx] == latest_stat_date:
                    values[idx] = price
                else:
                    dates.insert(idx, latest_stat_date)
                    values.insert(idx, price)
                if ticker in tickers_without_price_history:
                    tickers_without_price_history.remove(ticker)

    def price_at_or_before(ticker: str, target: date) -> Decimal | None:
        dates = price_dates.get(ticker)
        if not dates:
            return None
        idx = bisect.bisect_right(dates, target) - 1
        if idx < 0:
            return None
        return price_values[ticker][idx]

    # Price and value every currently-held position using the same fresh data
    # just fetched above (history close, or the live-quote patch for today) -
    # falling back to whatever price was already stored only if Yahoo had
    # nothing at all for that ticker (network problem, delisted, bad symbol).
    total_market_value = ZERO
    for position in computed_positions:
        data = positions[position.ticker]
        fresh_price = price_at_or_before(position.ticker, date.today())
        price = fresh_price if fresh_price is not None else decimal_or_zero(data["current_price"])
        rate = latest_rate(db, position.currency)
        market_value = position.quantity * price * rate
        invested = decimal_or_zero(position.invested_czk)
        profit = market_value - invested
        position.current_price = as_decimal(price)
        position.market_value_czk = market_value
        position.profit_czk = profit
        position.profit_pct = (profit / invested) if invested else None
        total_market_value += market_value

    for position in computed_positions:
        position.portfolio_share_pct = (
            decimal_or_zero(position.market_value_czk) / total_market_value if total_market_value else None
        )

    ticker_running_qty: dict[str, Decimal] = defaultdict(Decimal)
    ticker_running_invested: dict[str, Decimal] = defaultdict(Decimal)
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
        realized_profit_total += daily_realized_profit[stat_date]
        eur_rate = rate_for_day(db, "EUR", stat_date)
        usd_rate = rate_for_day(db, "USD", stat_date)
        eur_in_czk = total_eur * eur_rate
        usd_in_czk = total_usd * usd_rate

        for ticker, events in ticker_qty_events.items():
            change = events.get(stat_date)
            if change:
                ticker_running_qty[ticker] += change
        for ticker, events in ticker_invested_events.items():
            change = events.get(stat_date)
            if change:
                ticker_running_invested[ticker] += change

        def day_rate_for(currency: str) -> Decimal:
            if currency == "EUR":
                return eur_rate
            if currency == "USD":
                return usd_rate
            return ONE

        # Market value of everything actually held on this day, bucketed by
        # trading currency - not the invested/purchased amount. Also flags
        # three kinds of "Upozorneni" (AkcieStatistika.bas), matching the
        # original Excel workbook's per-day Info column exactly: a position
        # down more than `drop_threshold` versus its own cost basis AS OF
        # THAT DAY ("T:"), a day-over-day price move past `alert_threshold`
        # ("D:"), or a ticker held that day with no price data at all for it.
        value_eur = ZERO
        value_usd = ZERO
        value_czk = ZERO
        movers: list[str] = []
        drawdowns: list[str] = []
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

            invested = ticker_running_invested.get(ticker, ZERO)
            if invested:
                market_value_czk = holding_value * day_rate_for(ticker_currency)
                drawdown_pct = (market_value_czk - invested) / invested * Decimal("100")
                if drawdown_pct <= -drop_threshold:
                    drawdowns.append(f"{ticker} (T:{drawdown_pct:+.1f}%)")

        watchlist_breaches: list[str] = []
        for watch in watchlist_rows:
            ticker = normalize_ticker(watch.ticker, None)
            if not ticker:
                continue
            price = price_at_or_before(ticker, stat_date)
            if price is None or price > watch.limit_price:
                continue
            watchlist_breaches.append(f"{ticker} (L:{price:.2f}/{watch.limit_price:.2f})")

        alert_bits = []
        if drawdowns or watchlist_breaches:
            alert_bits.append(", ".join([*drawdowns, *watchlist_breaches]))
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
                portfolio_id=portfolio_id,
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
                realized_profit_czk=daily_realized_profit[stat_date],
                realized_profit_total_czk=realized_profit_total,
                profit_pct=(
                    ((total_value_czk + dividends_total - invested_total) / invested_total) if invested_total else None
                ),
                daily_profit_czk=daily_profit,
                alerts=day_alerts,
            )
        )

    if not dry_run:
        db.query(PortfolioPosition).filter(PortfolioPosition.portfolio_id == portfolio_id).delete()
        for position in computed_positions:
            db.add(position)
        stats_query = db.query(DailyStatistic).filter(DailyStatistic.portfolio_id == portfolio_id)
        if date_from is not None:
            stats_query.filter(DailyStatistic.stat_date >= date_from).delete()
        else:
            stats_query.delete()
        for statistic in computed_stats:
            db.add(statistic)
        db.commit()

    return {
        "dry_run": dry_run,
        "date_from": date_from,
        "transactions": len(transactions),
        "portfolio_positions": len(computed_positions),
        "daily_statistics": len(computed_stats),
        "price_fetch_failures": len(tickers_without_price_history),
        "price_fetch_failed_tickers": sorted(tickers_without_price_history)[:25],
        # The full range that was actually considered (independent of date_from,
        # which only controls what gets (re)written) - lets the caller explain
        # a 0-row result precisely instead of just guessing why nothing changed.
        "computed_range_from": stat_dates[0] if stat_dates else None,
        "computed_range_to": calendar_dates[-1] if calendar_dates else None,
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
    for url in urls:
        try:
            payload = _yahoo_get(url)
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


def build_ticker_history(db: Session, portfolio_id: uuid.UUID, ticker: str, date_from: date, date_to: date) -> dict[str, Any]:
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
        .where(
            StockTransaction.portfolio_id == portfolio_id,
            StockTransaction.ticker == normalized,
            StockTransaction.traded_on.is_not(None),
        )
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
        # Stored as a fraction (0.0266 = 2.66%), matching every other *_pct
        # field in the app (profit_pct, difference_pct, ...) so the frontend's
        # shared percent formatter (Intl "percent" style) renders it correctly.
        change_pct = ((price_cm - previous_price_cm) / previous_price_cm) if previous_price_cm else None
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
