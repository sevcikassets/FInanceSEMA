from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
import unicodedata
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

try:
    from zoneinfo import ZoneInfo

    PRAGUE_TZ: Any = ZoneInfo("Europe/Prague")
except Exception:  # tzdata not installed - fall back to naive local time
    PRAGUE_TZ = None

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from .auth import ALL_AGENDAS, authenticate, create_token, hash_password, require_user, verify_password
from .config import get_settings
from .db import Base, engine, get_db
from .excel_import import import_workbooks
from .loan_calc import project_annual_interest
from .models import AppUser, Asset, AssetCost, DailyStatistic, ExchangeRate, LoanMovement, Party, PortfolioPosition, StockTransaction, WatchlistStock
from .stock_services import (
    build_ticker_history,
    compute_alerts,
    import_patria_trades,
    recalculate_stocks,
    refresh_current_prices,
)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInput(BaseModel):
    username: str
    password: str
    full_name: str | None = None
    is_admin: bool = False
    allowed_agendas: list[str] = []


class ExchangeRateInput(BaseModel):
    rate_date: date
    currency: str
    rate_to_czk: Decimal


class PatriaImportInput(BaseModel):
    text: str


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def model_dict(row: Any) -> dict[str, Any]:
    data = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        data[col.name] = json_value(value)
    return data


def computed_interest_plan(asset: Asset) -> dict[str, Any]:
    projection = project_annual_interest(
        borrowed_amount=asset.borrowed_amount,
        interest_rate=asset.interest_rate,
        borrowed_from=asset.borrowed_from,
        loan_years=asset.loan_years,
        borrowed_to=asset.borrowed_to,
    )
    return {str(year): json_value(value) for year, value in sorted(projection.items())}


def user_dict(row: AppUser) -> dict[str, Any]:
    return {
        "username": row.username,
        "full_name": row.full_name,
        "is_active": row.is_active,
        "is_admin": row.is_admin,
        "allowed_agendas": row.allowed_agendas or [],
    }


def ensure_admin_user() -> None:
    db = next(get_db())
    try:
        row = db.get(AppUser, settings.app_username)
        if row is None:
            db.add(
                AppUser(
                    username=settings.app_username,
                    password_hash=hash_password(settings.app_password),
                    full_name="Administrátor",
                    is_active=True,
                    is_admin=True,
                    allowed_agendas=ALL_AGENDAS,
                )
            )
            db.commit()
    finally:
        db.close()


def require_admin(username: str = Depends(require_user), db: Session = Depends(get_db)) -> str:
    user = db.get(AppUser, username)
    if user is not None:
        if user.is_active and user.is_admin:
            return username
        raise HTTPException(status_code=403, detail="Pouze administrátor může spravovat uživatele")
    if username == settings.app_username:
        return username
    raise HTTPException(status_code=403, detail="Pouze administrátor může spravovat uživatele")


def normalize_match_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(part for part in text.lower().replace("-", " ").split() if part not in {"byt"})


def source_sheet_matches_asset(source_sheet: str | None, asset_name: str | None) -> bool:
    tokens = normalize_match_text(source_sheet).split()
    asset_text = normalize_match_text(asset_name)
    return bool(tokens) and all(token in asset_text for token in tokens)


def upsert_rate(db: Session, rate_date: date, currency: str, rate_to_czk: Decimal) -> ExchangeRate:
    currency = currency.strip().upper()
    row = db.scalar(select(ExchangeRate).where(ExchangeRate.rate_date == rate_date, ExchangeRate.currency == currency))
    if row is None:
        row = ExchangeRate(rate_date=rate_date, currency=currency, rate_to_czk=rate_to_czk)
        db.add(row)
    else:
        row.rate_to_czk = rate_to_czk
    db.flush()
    return row


def fetch_cnb_rates(rate_date: date) -> list[dict[str, Any]]:
    params = urlencode({"date": rate_date.strftime("%d.%m.%Y")})
    url = f"https://www.cnb.cz/cs/financni_trhy/devizovy_trh/kurzy_devizoveho_trhu/denni_kurz.txt?{params}"
    try:
        with urlopen(url, timeout=15) as response:
            text = response.read().decode("windows-1250")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CNB download failed: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5 or parts[0] == "země":
            continue
        # A malformed/unexpected line (CNB has occasionally shipped odd rows
        # for withdrawn currencies or formatting quirks) must not blow up the
        # whole recalculation - skip just that line instead of raising.
        try:
            amount = Decimal(parts[2].replace(",", "."))
            code = parts[3].strip().upper()
            rate = Decimal(parts[4].replace(",", ".")) / amount
        except (InvalidOperation, ZeroDivisionError, IndexError):
            continue
        rows.append({"rate_date": rate_date, "currency": code, "rate_to_czk": rate})
    if not rows:
        raise HTTPException(status_code=502, detail="CNB response did not contain exchange rates")
    return rows


def cnb_cutoff_date() -> date:
    """CNB publishes each day's rate around 14:30 Prague time - before that,
    only yesterday's rate is available yet. Mirrors KurzyCNB.bas's
    FetchCNBIfNeeded ("pred 14:30 -> do vcera, po 14:30 -> vcetne dneska")."""
    now = datetime.now(PRAGUE_TZ) if PRAGUE_TZ else datetime.now()
    if (now.hour, now.minute) >= (14, 30):
        return now.date()
    return now.date() - timedelta(days=1)


def ensure_cnb_rates_up_to_date(db: Session) -> int:
    """Auto-fetch any missing EUR/USD CNB rates up to the publish cutoff, so a
    recalculation always uses current exchange rates without a separate manual
    "Kurzy CNB" step - mirrors AktualizujStatistiku's automatic call to
    FetchCNBIfNeeded() at the start of every recompute. If either currency has
    no rates stored at all yet, this is skipped (same as the VBA's empty-sheet
    guard) - a first manual fetch is expected to seed the history.
    """
    last_dates: list[date] = []
    for currency in ("EUR", "USD"):
        row = db.scalar(
            select(ExchangeRate).where(ExchangeRate.currency == currency).order_by(desc(ExchangeRate.rate_date)).limit(1)
        )
        if row is None:
            return 0
        last_dates.append(row.rate_date)

    cutoff = cnb_cutoff_date()
    check_date = min(last_dates) + timedelta(days=1)
    added = 0
    while check_date <= cutoff:
        if check_date.weekday() < 5:  # CNB doesn't publish on weekends
            try:
                rows = fetch_cnb_rates(check_date)
            except Exception:
                # A single day's CNB request failing (network hiccup, malformed
                # response, unexpected format) must not take down the whole
                # recalculation - the rest of the range still gets a chance,
                # and this day's rate can simply be filled in on a later run.
                rows = []
            for row in rows:
                if row["currency"] in ("EUR", "USD"):
                    upsert_rate(db, row["rate_date"], row["currency"], row["rate_to_czk"])
                    added += 1
        check_date += timedelta(days=1)
    if added:
        db.commit()
    return added


logger = logging.getLogger(__name__)

app = FastAPI(title="FinanceSEMA API")
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_admin_user()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    user = db.get(AppUser, payload.username)
    valid_db_user = bool(user and user.is_active and verify_password(payload.password, user.password_hash))
    if not valid_db_user and not authenticate(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"token": create_token(payload.username)}


@app.get("/auth/me")
def me(username: str = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.get(AppUser, username)
    if user is None:
        return {
            "username": username,
            "full_name": None,
            "is_active": True,
            "is_admin": username == settings.app_username,
            "allowed_agendas": ALL_AGENDAS,
        }
    return user_dict(user)


@app.get("/users")
def users(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(AppUser).order_by(AppUser.username)).all()
    return [user_dict(row) for row in rows]


@app.post("/users")
def create_user(payload: UserInput, _: str = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    username = payload.username.strip()
    if not username or not payload.password:
        raise HTTPException(status_code=400, detail="Uživatelské jméno a heslo jsou povinné")
    if db.get(AppUser, username) is not None:
        raise HTTPException(status_code=409, detail="Uživatel už existuje")
    allowed = [agenda for agenda in payload.allowed_agendas if agenda in ALL_AGENDAS]
    if payload.is_admin:
        allowed = ALL_AGENDAS
    row = AppUser(
        username=username,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        is_active=True,
        is_admin=payload.is_admin,
        allowed_agendas=allowed,
    )
    db.add(row)
    db.commit()
    return user_dict(row)


@app.get("/summary")
def summary(_: str = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    latest_stat = db.scalar(select(DailyStatistic).order_by(desc(DailyStatistic.stat_date)).limit(1))
    loan_total = db.scalar(select(func.coalesce(func.sum(LoanMovement.amount), 0)))
    asset_total = db.scalar(select(func.coalesce(func.sum(Asset.total_value), 0)))
    cost_total = db.scalar(select(func.coalesce(func.sum(AssetCost.amount), 0)))
    portfolio_value = db.scalar(select(func.coalesce(func.sum(PortfolioPosition.market_value_czk), 0)))
    portfolio_profit = db.scalar(select(func.coalesce(func.sum(PortfolioPosition.profit_czk), 0)))
    return {
        "loans_total": json_value(loan_total),
        "assets_total": json_value(asset_total),
        "asset_costs_total": json_value(cost_total),
        "portfolio_value_czk": json_value(portfolio_value),
        "portfolio_profit_czk": json_value(portfolio_profit),
        "latest_stat": model_dict(latest_stat) if latest_stat else None,
        "counts": {
            "loan_movements": db.scalar(select(func.count()).select_from(LoanMovement)),
            "assets": db.scalar(select(func.count()).select_from(Asset)),
            "asset_costs": db.scalar(select(func.count()).select_from(AssetCost)),
            "stock_transactions": db.scalar(select(func.count()).select_from(StockTransaction)),
            "watchlist_stocks": db.scalar(select(func.count()).select_from(WatchlistStock)),
            "portfolio_positions": db.scalar(select(func.count()).select_from(PortfolioPosition)),
            "daily_statistics": db.scalar(select(func.count()).select_from(DailyStatistic)),
            "exchange_rates": db.scalar(select(func.count()).select_from(ExchangeRate)),
        },
    }


@app.get("/loans/movements")
def loan_movements(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(LoanMovement).order_by(desc(LoanMovement.movement_date).nullslast()).limit(500)).all()
    party_names = {p.id: p.name for p in db.scalars(select(Party)).all()}
    result = []
    for row in rows:
        data = model_dict(row)
        data["lender"] = party_names.get(row.lender_id)
        data["borrower"] = party_names.get(row.borrower_id)
        result.append(data)
    return result


@app.get("/assets")
def assets(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    owners = {p.id: p.name for p in db.scalars(select(Party)).all()}
    rows = db.scalars(select(Asset).order_by(Asset.code)).all()
    return [
        model_dict(row) | {"owner": owners.get(row.owner_id), "computed_interest_plan": computed_interest_plan(row)}
        for row in rows
    ]


@app.get("/assets/{asset_id}/interest-projection")
def asset_interest_projection(
    asset_id: str, _: str = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Majetek nenalezen")
    projection = computed_interest_plan(asset)
    return {
        "asset_id": str(asset.id),
        "asset_code": asset.code,
        "imported_plan": asset.annual_interest_plan or {},
        "computed_plan": projection,
        "total_computed": round(sum(projection.values()), 2) if projection else 0,
    }


@app.get("/assets/agendas")
def asset_agendas(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    owners = {p.id: p.name for p in db.scalars(select(Party)).all()}
    payers = {p.id: p.name for p in db.scalars(select(Party)).all()}
    assets = db.scalars(select(Asset).order_by(Asset.code)).all()
    result = []
    for asset in assets:
        linked_costs = db.scalars(
            select(AssetCost)
            .where(AssetCost.asset_id == asset.id)
            .order_by(desc(AssetCost.cost_date).nullslast(), AssetCost.source_row)
        ).all()
        unlinked_costs = [
            cost
            for cost in db.scalars(select(AssetCost).where(AssetCost.asset_id.is_(None))).all()
            if source_sheet_matches_asset(cost.source_sheet, asset.name)
        ]
        costs = sorted(
            [*linked_costs, *unlinked_costs],
            key=lambda cost: (cost.cost_date is None, cost.cost_date or date.min, cost.source_row or 0),
            reverse=True,
        )
        cost_total = sum((cost.amount or Decimal("0")) for cost in costs)
        categories: dict[str, Decimal] = {}
        for cost in costs:
            key = cost.category or "Bez kategorie"
            categories[key] = categories.get(key, Decimal("0")) + (cost.amount or Decimal("0"))
        asset_data = model_dict(asset)
        asset_data["owner"] = owners.get(asset.owner_id)
        asset_data["computed_interest_plan"] = computed_interest_plan(asset)
        result.append(
            {
                "asset": asset_data,
                "cost_total": json_value(cost_total),
                "cost_count": len(costs),
                "costs": [
                    model_dict(cost)
                    | {
                        "asset": asset.name,
                        "payer": payers.get(cost.payer_id),
                    }
                    for cost in costs
                ],
                "categories": [{"category": key, "amount": json_value(value)} for key, value in sorted(categories.items())],
            }
        )
    return result


@app.get("/assets/costs")
def asset_costs(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(AssetCost).order_by(desc(AssetCost.cost_date).nullslast()).limit(500)).all()
    assets_by_id = {a.id: a.name for a in db.scalars(select(Asset)).all()}
    payers = {p.id: p.name for p in db.scalars(select(Party)).all()}
    return [model_dict(row) | {"asset": assets_by_id.get(row.asset_id), "payer": payers.get(row.payer_id)} for row in rows]


@app.get("/stocks/transactions")
def stock_transactions(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(StockTransaction).order_by(desc(StockTransaction.traded_on).nullslast()).limit(500)).all()
    return [model_dict(row) for row in rows]


@app.get("/stocks/portfolio")
def portfolio(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(PortfolioPosition).order_by(desc(PortfolioPosition.market_value_czk).nullslast())).all()
    return [model_dict(row) for row in rows]


@app.get("/stocks/watchlist")
def stock_watchlist(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(WatchlistStock).order_by(desc(WatchlistStock.watched_on).nullslast(), WatchlistStock.ticker)).all()
    return [model_dict(row) for row in rows]


@app.get("/stocks/statistics")
def stock_statistics(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(DailyStatistic).order_by(desc(DailyStatistic.stat_date)).limit(200)).all()
    return [model_dict(row) for row in rows]


@app.get("/stocks/overview")
def stock_overview(_: str = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    movements = db.execute(
        select(
            StockTransaction.movement_type,
            func.count().label("count"),
            func.coalesce(func.sum(StockTransaction.amount_czk), 0).label("amount_czk"),
        )
        .group_by(StockTransaction.movement_type)
        .order_by(StockTransaction.movement_type)
    ).all()
    currencies = db.execute(
        select(
            StockTransaction.currency,
            func.count().label("count"),
            func.coalesce(func.sum(StockTransaction.gross_amount_ccy), 0).label("amount_ccy"),
            func.coalesce(func.sum(StockTransaction.amount_czk), 0).label("amount_czk"),
        )
        .group_by(StockTransaction.currency)
        .order_by(StockTransaction.currency)
    ).all()
    top_profit = db.scalars(select(PortfolioPosition).order_by(desc(PortfolioPosition.profit_czk).nullslast()).limit(8)).all()
    top_loss = db.scalars(select(PortfolioPosition).order_by(PortfolioPosition.profit_czk.nullslast()).limit(8)).all()
    return {
        "movements": [
            {"movement_type": row.movement_type, "count": row.count, "amount_czk": json_value(row.amount_czk)}
            for row in movements
        ],
        "currencies": [
            {
                "currency": row.currency,
                "count": row.count,
                "amount_ccy": json_value(row.amount_ccy),
                "amount_czk": json_value(row.amount_czk),
            }
            for row in currencies
        ],
        "top_profit": [model_dict(row) for row in top_profit],
        "top_loss": [model_dict(row) for row in top_loss],
    }


@app.get("/stocks/alerts")
def stock_alerts(
    _: str = Depends(require_user),
    db: Session = Depends(get_db),
    threshold_pct: Decimal = Query(default=Decimal("10")),
) -> dict[str, Any]:
    return compute_alerts(db, threshold_pct=threshold_pct)


@app.get("/stocks/ticker-history")
def stock_ticker_history(
    ticker: str,
    date_from: date,
    date_to: date,
    _: str = Depends(require_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return build_ticker_history(db, ticker=ticker, date_from=date_from, date_to=date_to)


@app.post("/stocks/import-patria")
def import_patria(payload: PatriaImportInput, _: str = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Text z Patrie je prázdný")
    return import_patria_trades(db, payload.text)


@app.post("/stocks/refresh-prices")
def refresh_stock_prices(
    _: str = Depends(require_user),
    db: Session = Depends(get_db),
    threshold_pct: Decimal = Query(default=Decimal("10")),
) -> dict[str, Any]:
    return refresh_current_prices(db, threshold_pct=threshold_pct)


@app.post("/stocks/recalculate")
def recalculate_stock_data(
    _: str = Depends(require_user),
    db: Session = Depends(get_db),
    dry_run: bool = False,
    date_from: date | None = Query(default=None),
    threshold_pct: Decimal = Query(default=Decimal("10")),
) -> dict[str, Any]:
    # An unhandled exception here used to reach the browser as a bare 500
    # with no CORS headers (Starlette's outermost error handler sits above
    # CORSMiddleware) - the browser then reports it as a CORS failure
    # ("Failed to fetch") with no hint of the real cause. Catch broadly and
    # re-raise as a normal HTTPException instead, which *does* get CORS
    # headers and puts the actual error text in front of the user/logs.
    try:
        # Auto-fetch missing CNB rates first, same as AktualizujStatistiku always
        # does before recomputing - but only for a real (non-preview) run, so
        # "Kontrola (náhled)" keeps its "nothing gets saved" promise.
        cnb_rates_added = ensure_cnb_rates_up_to_date(db) if not dry_run else 0
        result = recalculate_stocks(db, dry_run=dry_run, date_from=date_from, threshold_pct=threshold_pct)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort safety net, see above
        db.rollback()
        logger.exception("recalculate_stock_data failed")
        raise HTTPException(status_code=500, detail=f"Přepočet selhal: {exc}") from exc
    result["cnb_rates_added"] = cnb_rates_added
    return result


@app.get("/rates")
def exchange_rates(
    _: str = Depends(require_user),
    db: Session = Depends(get_db),
    currency: str | None = None,
    limit: int = Query(default=300, ge=1, le=2000),
) -> list[dict[str, Any]]:
    query = select(ExchangeRate).order_by(desc(ExchangeRate.rate_date), ExchangeRate.currency).limit(limit)
    if currency:
        query = (
            select(ExchangeRate)
            .where(ExchangeRate.currency == currency.upper())
            .order_by(desc(ExchangeRate.rate_date))
            .limit(limit)
        )
    rows = db.scalars(query).all()
    return [model_dict(row) for row in rows]


@app.get("/rates/daily")
def daily_exchange_rates(
    _: str = Depends(require_user),
    db: Session = Depends(get_db),
    limit: int = Query(default=300, ge=1, le=2000),
) -> list[dict[str, Any]]:
    dates = db.scalars(select(ExchangeRate.rate_date).distinct().order_by(desc(ExchangeRate.rate_date)).limit(limit)).all()
    if not dates:
        return []
    rows = db.scalars(
        select(ExchangeRate)
        .where(ExchangeRate.rate_date.in_(dates), ExchangeRate.currency.in_(["EUR", "USD"]))
        .order_by(desc(ExchangeRate.rate_date), ExchangeRate.currency)
    ).all()
    by_date: dict[date, dict[str, Any]] = {d: {"rate_date": d, "eur": None, "usd": None} for d in dates}
    for row in rows:
        key = row.currency.lower()
        if key in ["eur", "usd"]:
            by_date[row.rate_date][key] = json_value(row.rate_to_czk)
    return list(by_date.values())


@app.get("/rates/latest")
def latest_exchange_rates(_: str = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    latest_date = db.scalar(select(func.max(ExchangeRate.rate_date)))
    if latest_date is None:
        return {"rate_date": None, "rates": []}
    rows = db.scalars(select(ExchangeRate).where(ExchangeRate.rate_date == latest_date).order_by(ExchangeRate.currency)).all()
    return {"rate_date": latest_date, "rates": [model_dict(row) for row in rows]}


@app.post("/rates")
def save_exchange_rate(payload: ExchangeRateInput, _: str = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = upsert_rate(db, payload.rate_date, payload.currency, payload.rate_to_czk)
    db.commit()
    return model_dict(row)


@app.post("/rates/fetch-cnb")
def fetch_exchange_rates_from_cnb(
    _: str = Depends(require_user),
    db: Session = Depends(get_db),
    rate_date: date | None = None,
) -> dict[str, Any]:
    target_date = rate_date or datetime.now().date()
    rows = fetch_cnb_rates(target_date)
    saved = [model_dict(upsert_rate(db, row["rate_date"], row["currency"], row["rate_to_czk"])) for row in rows]
    db.commit()
    return {"rate_date": target_date, "count": len(saved), "rates": saved}


@app.post("/imports/excel")
async def import_excel(
    finance: UploadFile = File(...),
    property_costs: UploadFile | None = File(None),
    _: str = Depends(require_user),
) -> dict[str, Any]:
    suffix = Path(finance.filename or "finance.xlsm").suffix or ".xlsm"
    with NamedTemporaryFile(delete=False, suffix=suffix) as f:
        finance_path = Path(f.name)
        f.write(await finance.read())
    property_path = None
    if property_costs is not None:
        with NamedTemporaryFile(delete=False, suffix=Path(property_costs.filename or "costs.xlsx").suffix or ".xlsx") as f:
            property_path = Path(f.name)
            f.write(await property_costs.read())
    counts = import_workbooks(finance_path, property_path)
    return {"status": "done", "counts": counts}
