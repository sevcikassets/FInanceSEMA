import base64
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import io
import json
import logging
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal
from urllib.parse import urlencode
from urllib.request import urlopen

try:
    from zoneinfo import ZoneInfo

    PRAGUE_TZ: Any = ZoneInfo("Europe/Prague")
except Exception:  # tzdata not installed - fall back to naive local time
    PRAGUE_TZ = None

import pyotp
import qrcode
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, select, text
from sqlalchemy.orm import Session

from .auth import (
    ALL_AGENDAS,
    GLOBAL_AGENDAS,
    PORTFOLIO_SCOPED_AGENDAS,
    authenticate,
    create_pending_2fa_token,
    create_token,
    hash_password,
    require_user,
    verify_password,
    verify_pending_2fa_token,
    verify_totp_code,
)
from .config import get_settings
from .db import Base, engine, get_db
from .excel_import import (
    get_or_create_asset_type,
    get_or_create_cost_category,
    get_or_create_party,
    import_workbooks,
    move_zapujcka_assets_to_loans,
    split_debt_assets_into_linked_liability,
)
from .loan_calc import amortization_schedule, project_annual_interest
from .models import (
    AppUser,
    Asset,
    AssetCost,
    AssetType,
    CostCategory,
    DailyStatistic,
    ExchangeRate,
    LoanMovement,
    MonthlyEvaluation,
    MonthlyEvaluationAssetCashflow,
    Party,
    Portfolio,
    PortfolioAccess,
    PortfolioPosition,
    PortfolioSelfParty,
    StockTransaction,
    WatchlistStock,
)
from .stock_services import (
    build_ticker_history,
    compute_alerts,
    import_patria_trades,
    movement_is_buy,
    movement_is_sell,
    recalculate_stocks,
    refresh_current_prices,
)


class LoginRequest(BaseModel):
    username: str
    password: str


class TwoFactorLoginRequest(BaseModel):
    pending_token: str
    code: str


class TwoFactorConfirmRequest(BaseModel):
    secret: str
    code: str


class TwoFactorDisableRequest(BaseModel):
    password: str
    code: str


class NotificationSettingsInput(BaseModel):
    alert_daily_change_pct: Decimal | None = None
    alert_drop_pct: Decimal | None = None


class UserInput(BaseModel):
    username: str
    password: str
    full_name: str | None = None
    is_admin: bool = False
    allowed_agendas: list[str] = []


class UserUpdateInput(BaseModel):
    full_name: str | None = None
    is_admin: bool = False
    is_active: bool = True
    allowed_agendas: list[str] = []
    # Optional - only set if provided, so editing a user never requires
    # re-entering (or blanking out) their existing password.
    password: str | None = None


class ExchangeRateInput(BaseModel):
    rate_date: date
    currency: str
    rate_to_czk: Decimal


class PatriaImportInput(BaseModel):
    text: str


class PortfolioInput(BaseModel):
    name: str


class CostCategoryInput(BaseModel):
    name: str


class AssetCostInput(BaseModel):
    asset_id: uuid.UUID | None = None
    cost_date: date | None = None
    item: str
    category: str | None = None
    amount: Decimal | None = None
    supplier: str | None = None
    payer: str | None = None
    note: str | None = None


class LoanMovementInput(BaseModel):
    movement_date: date
    lender: str
    borrower: str
    amount: Decimal
    period_label: str | None = None
    interest_rate: Decimal | None = None
    interest_period: str | None = None
    planned_end_date: date | None = None
    completed_at: date | None = None
    description: str | None = None


# Kept in sync with the loan-related columns on Asset - used both to
# validate AssetTypeInput.required_fields and to drive the create/edit
# form's field checklist on the frontend.
ASSET_REQUIRED_FIELD_CHOICES = [
    "owner",
    "total_value",
    "own_funds",
    "borrowed_amount",
    "lender_name",
    "borrowed_from",
    "borrowed_to",
    "interest_rate",
    "loan_years",
    "fixed_until",
    "payment",
]


class AssetTypeInput(BaseModel):
    name: str
    calculation_mode: Literal["none", "debt_interest"] = "none"
    required_fields: list[str] = []


class PayerInput(BaseModel):
    name: str


class AssetInput(BaseModel):
    code: str
    name: str
    # Free text, resolved via get_or_create_party - asset ownership isn't a
    # managed dictionary (that's what "Plátci" manages now, for AssetCost's
    # payer field), so this works the same way it did before either registry
    # existed: type a name, reuse the existing Party if it already matches.
    owner: str | None = None
    asset_type_id: uuid.UUID | None = None
    linked_asset_id: uuid.UUID | None = None
    total_value: Decimal | None = None
    own_funds: Decimal | None = None
    borrowed_amount: Decimal | None = None
    lender_name: str | None = None
    borrowed_from: date | None = None
    borrowed_to: date | None = None
    interest_rate: Decimal | None = None
    loan_years: Decimal | None = None
    fixed_until: date | None = None
    payment: Decimal | None = None


class PortfolioAccessGrant(BaseModel):
    portfolio_id: uuid.UUID
    allowed_agendas: list[str] = []


class PortfolioAccessInput(BaseModel):
    grants: list[PortfolioAccessGrant] = []


class SelfPartiesInput(BaseModel):
    party_ids: list[uuid.UUID] = []


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


def computed_interest_plan(asset: Asset, calculation_mode: str | None) -> dict[str, Any]:
    """Annual interest projection - only meaningful for a "debt_interest"
    typed asset (a Hypotéka: borrowed_amount is money OWED, interest is an
    expense). calculation_mode is passed in explicitly (not lazy-loaded via
    a relationship) so callers can prefetch it once per request instead of
    N+1-querying AssetType per row."""
    if calculation_mode != "debt_interest":
        return {}
    projection = project_annual_interest(
        borrowed_amount=asset.borrowed_amount,
        interest_rate=asset.interest_rate,
        borrowed_from=asset.borrowed_from,
        loan_years=asset.loan_years,
        borrowed_to=asset.borrowed_to,
    )
    return {str(year): json_value(value) for year, value in sorted(projection.items())}


def asset_net_worth_contribution(asset: Asset, calculation_mode: str | None) -> Decimal:
    """How much this asset counts toward /summary's assets_total: a
    debt_interest asset (Hypotéka) is money OWED, so its borrowed_amount
    reduces net worth; everything else counts its total_value as-is."""
    if calculation_mode == "debt_interest":
        return -(asset.borrowed_amount or Decimal("0"))
    return asset.total_value or Decimal("0")


def loan_movement_interest_plan(movement: LoanMovement) -> dict[str, Any]:
    """Same amortization projection as computed_interest_plan, applied to a
    LoanMovement instead of an Asset - every movement with amount/
    interest_rate/movement_date filled is eligible (no per-movement "type"
    concept exists, unlike Asset/AssetType)."""
    projection = project_annual_interest(
        borrowed_amount=movement.amount,
        interest_rate=movement.interest_rate,
        borrowed_from=movement.movement_date,
        borrowed_to=movement.planned_end_date,
    )
    return {str(year): json_value(value) for year, value in sorted(projection.items())}


def amortization_interest_in_period(
    pv: Decimal | None,
    interest_rate: Decimal | None,
    start_date: date | None,
    loan_years: Decimal | None,
    end_date: date | None,
    period: str,
) -> Decimal:
    """Interest accrued within a single "YYYY-MM" period, via the same
    amortization_schedule() used by the payment-schedule endpoints - an
    exact period-by-period breakdown, not an annual/12 approximation.
    Missing inputs (no rate/term) yield an empty schedule, contributing 0 -
    same "missing inputs -> nothing" contract amortization_schedule already
    has, no special-casing needed here."""
    schedule = amortization_schedule(pv=pv, interest_rate=interest_rate, start_date=start_date, loan_years=loan_years, end_date=end_date)
    return sum((p["interest"] for p in schedule if p["date"].strftime("%Y-%m") == period), Decimal("0"))


def period_bounds(period: str) -> tuple[date, date]:
    """(first day, last day) of a "YYYY-MM" period."""
    year, month = int(period[:4]), int(period[5:7])
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def classify_loan_interest(
    db: Session, portfolio_id: uuid.UUID, self_party_ids: set[uuid.UUID], period: str
) -> tuple[Decimal, Decimal, list[dict[str, Any]]]:
    """Classifies every LoanMovement's interest for `period` as received,
    paid, or excluded (both sides self = internal transfer; neither side
    self = not this Subjekt's business), per PortfolioSelfParty. Also folds
    in Hypotéka-typed Assets (always "paid" - a mortgage is always our own
    liability, no self-party ambiguity - matches computed_interest_plan's
    existing "debt_interest = paid" framing).

    Returns (total_received, total_paid, detail) where detail is a flat list
    of every non-zero contributor - used both for the stored aggregate
    (compute_monthly_evaluation, which only keeps the two totals) and the
    on-demand interest-detail endpoint (recomputed live, never persisted,
    since it's cheap and re-deriving avoids a second child table to keep in
    sync with the aggregate)."""
    interest_received = Decimal("0")
    interest_paid = Decimal("0")
    detail: list[dict[str, Any]] = []

    party_names = {p.id: p.name for p in db.scalars(select(Party)).all()}
    for movement in db.scalars(select(LoanMovement).where(LoanMovement.portfolio_id == portfolio_id)).all():
        lender_is_self = movement.lender_id in self_party_ids
        borrower_is_self = movement.borrower_id in self_party_ids
        if lender_is_self == borrower_is_self:
            continue  # both self (internal transfer) or neither (not this Subjekt's business) -> excluded
        interest = amortization_interest_in_period(
            movement.amount, movement.interest_rate, movement.movement_date, None, movement.planned_end_date, period
        )
        if interest == 0:
            continue
        if lender_is_self:
            interest_received += interest
            counterparty = party_names.get(movement.borrower_id)
        else:
            interest_paid += interest
            counterparty = party_names.get(movement.lender_id)
        detail.append(
            {
                "direction": "received" if lender_is_self else "paid",
                "source": "loan",
                "counterparty": counterparty,
                "description": movement.description,
                "interest_czk": json_value(interest),
            }
        )

    asset_types = {t.id: t for t in db.scalars(select(AssetType).where(AssetType.portfolio_id == portfolio_id)).all()}
    for asset in db.scalars(select(Asset).where(Asset.portfolio_id == portfolio_id)).all():
        asset_type = asset_types.get(asset.asset_type_id)
        if asset_type is None or asset_type.calculation_mode != "debt_interest":
            continue
        interest = amortization_interest_in_period(
            asset.borrowed_amount, asset.interest_rate, asset.borrowed_from, asset.loan_years, asset.borrowed_to, period
        )
        if interest == 0:
            continue
        interest_paid += interest
        detail.append(
            {"direction": "paid", "source": "asset", "counterparty": asset.name, "description": asset.code, "interest_czk": json_value(interest)}
        )

    return interest_received, interest_paid, detail


def compute_monthly_evaluation(db: Session, portfolio_id: uuid.UUID, period: str) -> MonthlyEvaluation:
    """Computes and upserts the Vyhodnocení for one Subjekt/period - see
    MonthlyEvaluation's docstring in models.py for what each figure means.
    Idempotent: rerunning for the same period overwrites that period's row
    and fully replaces its MonthlyEvaluationAssetCashflow children, it never
    accumulates across reruns."""
    period_start, period_end = period_bounds(period)

    self_party_ids = {
        row.party_id for row in db.scalars(select(PortfolioSelfParty).where(PortfolioSelfParty.portfolio_id == portfolio_id)).all()
    }
    interest_received, interest_paid, _ = classify_loan_interest(db, portfolio_id, self_party_ids, period)

    stock_income = Decimal("0")
    stock_expense = Decimal("0")
    for transaction in db.scalars(
        select(StockTransaction).where(
            StockTransaction.portfolio_id == portfolio_id, StockTransaction.traded_on.between(period_start, period_end)
        )
    ).all():
        amount = transaction.amount_czk or Decimal("0")
        if movement_is_buy(transaction.movement_type):
            stock_expense += amount
        elif movement_is_sell(transaction.movement_type):
            stock_income += amount

    realized_profit = db.scalar(
        select(func.coalesce(func.sum(DailyStatistic.realized_profit_czk), 0))
        .where(DailyStatistic.portfolio_id == portfolio_id, DailyStatistic.stat_date.between(period_start, period_end))
    )
    dividends = db.scalar(
        select(func.coalesce(func.sum(DailyStatistic.dividends), 0))
        .where(DailyStatistic.portfolio_id == portfolio_id, DailyStatistic.stat_date.between(period_start, period_end))
    )
    unrealized_end = db.scalar(
        select(DailyStatistic.unrealized_profit_czk)
        .where(DailyStatistic.portfolio_id == portfolio_id, DailyStatistic.stat_date <= period_end)
        .order_by(desc(DailyStatistic.stat_date))
        .limit(1)
    )
    unrealized_before = db.scalar(
        select(DailyStatistic.unrealized_profit_czk)
        .where(DailyStatistic.portfolio_id == portfolio_id, DailyStatistic.stat_date < period_start)
        .order_by(desc(DailyStatistic.stat_date))
        .limit(1)
    )
    unrealized_delta = (unrealized_end or Decimal("0")) - (unrealized_before or Decimal("0"))

    evaluation = db.scalar(
        select(MonthlyEvaluation).where(MonthlyEvaluation.portfolio_id == portfolio_id, MonthlyEvaluation.period == period)
    )
    if evaluation is None:
        evaluation = MonthlyEvaluation(portfolio_id=portfolio_id, period=period)
        db.add(evaluation)
    evaluation.interest_received_czk = interest_received
    evaluation.interest_paid_czk = interest_paid
    evaluation.realized_profit_czk = realized_profit
    evaluation.unrealized_profit_delta_czk = unrealized_delta
    evaluation.dividends_czk = dividends
    evaluation.stock_income_czk = stock_income
    evaluation.stock_expense_czk = stock_expense
    evaluation.computed_at = datetime.now(timezone.utc)
    db.flush()

    db.execute(delete(MonthlyEvaluationAssetCashflow).where(MonthlyEvaluationAssetCashflow.evaluation_id == evaluation.id))
    cashflow_rows = db.execute(
        select(
            AssetCost.asset_id,
            func.coalesce(func.sum(func.greatest(AssetCost.amount, 0)), 0).label("expense"),
            func.coalesce(-func.sum(func.least(AssetCost.amount, 0)), 0).label("income"),
        )
        .where(AssetCost.portfolio_id == portfolio_id, AssetCost.cost_date.between(period_start, period_end))
        .group_by(AssetCost.asset_id)
    ).all()
    for asset_id, expense, income in cashflow_rows:
        db.add(
            MonthlyEvaluationAssetCashflow(
                evaluation_id=evaluation.id, asset_id=asset_id, income_czk=income, expense_czk=expense
            )
        )
    db.commit()
    return evaluation


DEFAULT_ALERT_THRESHOLD_PCT = Decimal("10")


def user_dict(row: AppUser) -> dict[str, Any]:
    return {
        "username": row.username,
        "full_name": row.full_name,
        "is_active": row.is_active,
        "is_admin": row.is_admin,
        "allowed_agendas": row.allowed_agendas or [],
        "totp_enabled": bool(row.totp_enabled),
        "alert_daily_change_pct": json_value(row.alert_daily_change_pct),
        "alert_drop_pct": json_value(row.alert_drop_pct),
    }


def portfolio_dict(row: Portfolio) -> dict[str, Any]:
    return {"id": str(row.id), "name": row.name}


def cost_attachment_path(cost_id: uuid.UUID) -> Path:
    # Filename is always the cost's own UUID, never a user-supplied name -
    # rules out path traversal entirely (see settings.attachments_dir).
    return Path(settings.attachments_dir) / f"{cost_id}.pdf"


def user_portfolios(username: str, db: Session) -> list[dict[str, Any]]:
    """Every Subjekt this user may see, with the agendas granted within it.
    Admins (including the env-var bootstrap admin, which may have no AppUser
    row) get every existing Subjekt with full PORTFOLIO_SCOPED_AGENDAS,
    mirroring how they already bypass AppUser.allowed_agendas entirely."""
    portfolios = db.scalars(select(Portfolio).order_by(Portfolio.name)).all()
    if is_admin_user(username, db):
        return [{"id": str(p.id), "name": p.name, "allowed_agendas": PORTFOLIO_SCOPED_AGENDAS} for p in portfolios]
    grants = {
        grant.portfolio_id: grant.allowed_agendas or []
        for grant in db.scalars(select(PortfolioAccess).where(PortfolioAccess.username == username)).all()
    }
    return [{"id": str(p.id), "name": p.name, "allowed_agendas": grants[p.id]} for p in portfolios if p.id in grants]


def resolve_threshold(db: Session, username: str, explicit: Decimal | None, field: str) -> Decimal:
    """Effective alert threshold (%) for the current user: an explicit query
    param always wins, otherwise the user's own saved preference (Nastaveni
    tab), otherwise the app-wide default. `field` is "alert_daily_change_pct"
    or "alert_drop_pct" on AppUser."""
    if explicit is not None:
        return explicit
    user = db.get(AppUser, username)
    saved = getattr(user, field, None) if user is not None else None
    return saved if saved is not None else DEFAULT_ALERT_THRESHOLD_PCT


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


def is_admin_user(username: str, db: Session) -> bool:
    user = db.get(AppUser, username)
    if user is not None:
        return bool(user.is_active and user.is_admin)
    return username == settings.app_username


def require_portfolio_access(agenda: str | tuple[str, ...] | None):
    """Dependency factory - every portfolio-scoped endpoint depends on
    `require_portfolio_access("assets")` etc. `agenda=None` means "any agenda
    granted on this Subjekt" (cross-agenda endpoints like /summary); a tuple
    means "any of these" (one endpoint backing two tabs, e.g.
    /stocks/transactions backs both "Transakce" and "Sledování akcie").
    Admins (and the env-var bootstrap admin, which may have no AppUser row)
    bypass the PortfolioAccess check entirely, mirroring require_admin."""
    agendas = (agenda,) if isinstance(agenda, str) else agenda

    def _dependency(
        portfolio_id: uuid.UUID = Query(...),
        username: str = Depends(require_user),
        db: Session = Depends(get_db),
    ) -> uuid.UUID:
        if is_admin_user(username, db):
            return portfolio_id
        grant = db.get(PortfolioAccess, (username, portfolio_id))
        granted = grant.allowed_agendas or [] if grant is not None else []
        ok = bool(granted) if agendas is None else any(agenda in granted for agenda in agendas)
        if not ok:
            raise HTTPException(status_code=403, detail="Nemáte přístup k tomuto subjektu")
        return portfolio_id

    return _dependency


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


def _constraint_exists(conn, table: str, name: str) -> bool:
    return (
        conn.execute(
            text("SELECT 1 FROM information_schema.table_constraints WHERE table_name = :t AND constraint_name = :n"),
            {"t": table, "n": name},
        ).scalar()
        is not None
    )


def ensure_schema_upgrades() -> None:
    """This project uses Base.metadata.create_all (creates missing tables
    only, never alters existing ones) instead of a real migration tool, so
    columns added after the first deploy need an explicit, idempotent
    ALTER TABLE here. Safe to run on every startup."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS totp_secret VARCHAR(64)"))
        conn.execute(text("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS alert_daily_change_pct NUMERIC(6, 3)"))
        conn.execute(text("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS alert_drop_pct NUMERIC(6, 3)"))

        # --- Subjekt (Portfolio) support ------------------------------------
        # `portfolios`/`portfolio_access` are brand-new tables - create_all
        # (called just before this function, see on_startup) already created
        # them, no manual DDL needed. Only ALTERs on pre-existing tables need
        # hand-written statements from here on.

        default_portfolio_id = conn.execute(text("SELECT id FROM portfolios ORDER BY created_at LIMIT 1")).scalar()
        if default_portfolio_id is None:
            default_portfolio_id = uuid.uuid4()
            conn.execute(
                text("INSERT INTO portfolios (id, name) VALUES (:id, :name)"),
                {"id": default_portfolio_id, "name": "Výchozí Subjekt"},
            )

        # Nullable-add -> backfill everything onto the default Subjekt ->
        # tighten to NOT NULL, for each of the five source-of-truth tables.
        for table in ("assets", "asset_costs", "loan_movements", "stock_transactions", "watchlist_stocks"):
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS portfolio_id UUID"))
            conn.execute(text(f"UPDATE {table} SET portfolio_id = :pid WHERE portfolio_id IS NULL"), {"pid": default_portfolio_id})
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN portfolio_id SET NOT NULL"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_portfolio_id ON {table} (portfolio_id)"))
            fk_name = f"fk_{table}_portfolio_id"
            if not _constraint_exists(conn, table, fk_name):
                conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {fk_name} FOREIGN KEY (portfolio_id) REFERENCES portfolios (id)"))

        # assets.code was globally unique; two Subjekty must be able to reuse
        # the same code (e.g. both importing a property-costs workbook, which
        # falls back to the fixed code "RD-KVASICE" - see excel_import.py),
        # so it becomes unique per (portfolio_id, code) instead.
        conn.execute(text("ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_code_key"))
        if not _constraint_exists(conn, "assets", "uq_assets_portfolio_code"):
            conn.execute(text("ALTER TABLE assets ADD CONSTRAINT uq_assets_portfolio_code UNIQUE (portfolio_id, code)"))

        # portfolio_positions/daily_statistics are fully disposable computed
        # caches - stock_services.recalculate_stocks wipes and rebuilds both
        # in full on every run - so rather than inventing a meaningless
        # "which Subjekt did this old cached row belong to" backfill, this
        # clears them once (guarded to run exactly once ever, via the
        # portfolio_id column-existence check below) and lets the next
        # "Přepočítat portfolio" repopulate them scoped to the default
        # Subjekt. This is NOT the same as the non-destructive guarantee
        # that applies to real user-entered data above - it's an
        # intentional one-time reset of a cache.
        for table, key_col in (("portfolio_positions", "ticker"), ("daily_statistics", "stat_date")):
            has_column = conn.execute(
                text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = 'portfolio_id'"),
                {"t": table},
            ).scalar()
            if has_column is None:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN portfolio_id UUID"))
                conn.execute(text(f"DELETE FROM {table}"))
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN portfolio_id SET NOT NULL"))
                pk_name = conn.execute(
                    text("SELECT constraint_name FROM information_schema.table_constraints WHERE table_name = :t AND constraint_type = 'PRIMARY KEY'"),
                    {"t": table},
                ).scalar()
                if pk_name:
                    conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {pk_name}"))
                conn.execute(text(f"ALTER TABLE {table} ADD PRIMARY KEY (portfolio_id, {key_col})"))
                fk_name = f"fk_{table}_portfolio_id"
                if not _constraint_exists(conn, table, fk_name):
                    conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {fk_name} FOREIGN KEY (portfolio_id) REFERENCES portfolios (id)"))

        # Seed PortfolioAccess for the default Subjekt from each non-admin
        # user's existing allowed_agendas, so nobody's effective access
        # silently changes on deploy day. Only inserts if the user has no
        # grant yet for this Subjekt - safe to rerun, and never overwrites an
        # admin's later edits via PUT /users/{username}/portfolio-access.
        users = conn.execute(text("SELECT username, allowed_agendas, is_admin FROM app_users")).all()
        for username, allowed_agendas, is_admin in users:
            if is_admin:
                continue
            granted = [agenda for agenda in (allowed_agendas or []) if agenda in PORTFOLIO_SCOPED_AGENDAS]
            if not granted:
                continue
            already_granted = conn.execute(
                text("SELECT 1 FROM portfolio_access WHERE username = :u AND portfolio_id = :p"),
                {"u": username, "p": default_portfolio_id},
            ).scalar()
            if already_granted is None:
                conn.execute(
                    text("INSERT INTO portfolio_access (username, portfolio_id, allowed_agendas) VALUES (:u, :p, CAST(:a AS JSONB))"),
                    {"u": username, "p": default_portfolio_id, "a": json.dumps(granted)},
                )

        # --- Cost categories: migrate legacy free-text AssetCost.category
        # into the new dictionary-backed category_id, so the "Kategorie
        # nákladů" agenda starts populated from whatever categories already
        # exist in imported cost data instead of empty. Idempotent - only
        # ever touches rows where category_id is still NULL, so once every
        # row has been backfilled this is a cheap no-op on every startup.
        conn.execute(text("ALTER TABLE asset_costs ADD COLUMN IF NOT EXISTS category_id UUID"))
        category_fk_name = "fk_asset_costs_category_id"
        if not _constraint_exists(conn, "asset_costs", category_fk_name):
            conn.execute(
                text(f"ALTER TABLE asset_costs ADD CONSTRAINT {category_fk_name} FOREIGN KEY (category_id) REFERENCES cost_categories (id)")
            )
        distinct_categories = conn.execute(
            text(
                "SELECT DISTINCT portfolio_id, category FROM asset_costs "
                "WHERE category IS NOT NULL AND category != '' AND category_id IS NULL"
            )
        ).all()
        for cat_portfolio_id, category_name in distinct_categories:
            existing_category_id = conn.execute(
                text("SELECT id FROM cost_categories WHERE portfolio_id = :p AND name = :n"),
                {"p": cat_portfolio_id, "n": category_name},
            ).scalar()
            if existing_category_id is None:
                existing_category_id = uuid.uuid4()
                conn.execute(
                    text("INSERT INTO cost_categories (id, portfolio_id, name) VALUES (:id, :p, :n)"),
                    {"id": existing_category_id, "p": cat_portfolio_id, "n": category_name},
                )
            conn.execute(
                text(
                    "UPDATE asset_costs SET category_id = :cid "
                    "WHERE portfolio_id = :p AND category = :n AND category_id IS NULL"
                ),
                {"cid": existing_category_id, "p": cat_portfolio_id, "n": category_name},
            )

        # --- Asset types / linked liabilities (Hypotéka) / Zápůjčka->Půjčky.
        # asset_types itself needs no manual DDL (brand-new table, create_all
        # handles it). Only the two new columns on the pre-existing `assets`
        # table need it.
        conn.execute(text("ALTER TABLE assets ADD COLUMN IF NOT EXISTS asset_type_id UUID"))
        asset_type_fk_name = "fk_assets_asset_type_id"
        if not _constraint_exists(conn, "assets", asset_type_fk_name):
            conn.execute(
                text(f"ALTER TABLE assets ADD CONSTRAINT {asset_type_fk_name} FOREIGN KEY (asset_type_id) REFERENCES asset_types (id)")
            )
        conn.execute(text("ALTER TABLE assets ADD COLUMN IF NOT EXISTS linked_asset_id UUID"))
        linked_asset_fk_name = "fk_assets_linked_asset_id"
        if not _constraint_exists(conn, "assets", linked_asset_fk_name):
            conn.execute(
                text(f"ALTER TABLE assets ADD CONSTRAINT {linked_asset_fk_name} FOREIGN KEY (linked_asset_id) REFERENCES assets (id)")
            )

        # --- Vyhodnocení (monthly evaluation): realized-gain tracking on the
        # pre-existing daily_statistics table. portfolio_self_parties/
        # monthly_evaluations/monthly_evaluation_asset_cashflows are brand-new
        # tables, create_all handles them - no manual DDL, and no backfill
        # loop either: realized profit fills in automatically the next time
        # "Přepočítat portfolio" runs (it always rebuilds daily_statistics
        # from scratch), and MonthlyEvaluation rows only ever come from an
        # explicit POST /evaluations/compute call.
        conn.execute(text("ALTER TABLE daily_statistics ADD COLUMN IF NOT EXISTS realized_profit_czk NUMERIC(20, 2)"))
        conn.execute(text("ALTER TABLE daily_statistics ADD COLUMN IF NOT EXISTS realized_profit_total_czk NUMERIC(20, 2)"))
        # monthly_evaluations itself was a brand-new table at the time these
        # two columns were added - not brand-new anymore for any deploy that
        # already ran the migration above once, so they need their own ALTER.
        conn.execute(text("ALTER TABLE monthly_evaluations ADD COLUMN IF NOT EXISTS stock_income_czk NUMERIC(20, 2) NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE monthly_evaluations ADD COLUMN IF NOT EXISTS stock_expense_czk NUMERIC(20, 2) NOT NULL DEFAULT 0"))

    # The data-shape migration below (moving rows, splitting one row into
    # two, copying several fields) is simpler and far less error-prone as
    # ORM object manipulation than hand-written INSERT/UPDATE/DELETE SQL -
    # unlike the raw-SQL blocks above, which are simple enough that SQL is
    # actually the more direct tool. Runs after the DDL above has committed
    # (the `with engine.begin()` block above just closed).
    with Session(bind=engine) as session:
        portfolio_ids = session.scalars(select(Portfolio.id)).all()

        # Order matters: "Zápůjčka" (money lent OUT) must move to Půjčky
        # BEFORE the type-dictionary backfill below, so no orphaned
        # "Zápůjčka" AssetType ever gets created - see
        # move_zapujcka_assets_to_loans's docstring in excel_import.py.
        for pid in portfolio_ids:
            move_zapujcka_assets_to_loans(session, pid)

        # Backfill AssetType from whatever asset_type text values remain
        # (Zápůjčka already gone via the step above) - same idempotent
        # get-or-create-then-link pattern as the cost-categories backfill.
        for pid in portfolio_ids:
            distinct_type_names = session.scalars(
                select(Asset.asset_type)
                .where(
                    Asset.portfolio_id == pid,
                    Asset.asset_type.isnot(None),
                    Asset.asset_type != "",
                    Asset.asset_type_id.is_(None),
                )
                .distinct()
            ).all()
            for type_name in distinct_type_names:
                asset_type = get_or_create_asset_type(session, pid, type_name)
                for asset in session.scalars(
                    select(Asset).where(
                        Asset.portfolio_id == pid, Asset.asset_type == type_name, Asset.asset_type_id.is_(None)
                    )
                ).all():
                    asset.asset_type_id = asset_type.id

        # Split any remaining Byt+loan combined row into Byt + linked
        # Hypotéka - see split_debt_assets_into_linked_liability's
        # docstring in excel_import.py for the exact candidate criteria.
        for pid in portfolio_ids:
            split_debt_assets_into_linked_liability(session, pid)

        session.commit()


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema_upgrades()
    ensure_admin_user()
    Path(settings.attachments_dir).mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = db.get(AppUser, payload.username)
    valid_db_user = bool(user and user.is_active and verify_password(payload.password, user.password_hash))
    if not valid_db_user and not authenticate(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if user is not None and user.totp_enabled:
        return {"requires_2fa": True, "pending_token": create_pending_2fa_token(payload.username)}
    return {"requires_2fa": False, "token": create_token(payload.username)}


@app.post("/auth/2fa/login")
def login_2fa(payload: TwoFactorLoginRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    username = verify_pending_2fa_token(payload.pending_token)
    user = db.get(AppUser, username)
    if user is None or not user.is_active or not user.totp_enabled or not user.totp_secret:
        raise HTTPException(status_code=401, detail="Dvoufázové ověření není pro tento účet zapnuté")
    if not verify_totp_code(user.totp_secret, payload.code):
        raise HTTPException(status_code=401, detail="Neplatný ověřovací kód")
    return {"token": create_token(username)}


@app.post("/auth/2fa/setup")
def setup_2fa(username: str = Depends(require_user), db: Session = Depends(get_db)) -> dict[str, str]:
    user = db.get(AppUser, username)
    if user is None:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    if user.totp_enabled:
        raise HTTPException(status_code=400, detail="Dvoufázové ověření je už zapnuté")
    # Nothing is persisted here - the secret only gets saved once /auth/2fa/confirm
    # proves the user actually scanned it into their authenticator app, so a
    # setup the user never finishes never leaves a half-configured account.
    secret = pyotp.random_base32()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name="FinanceSEMA")
    qr_image = qrcode.make(uri)
    buffer = io.BytesIO()
    qr_image.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"secret": secret, "otpauth_uri": uri, "qr_code_png_base64": qr_base64}


@app.post("/auth/2fa/confirm")
def confirm_2fa(
    payload: TwoFactorConfirmRequest, username: str = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    user = db.get(AppUser, username)
    if user is None:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    if not verify_totp_code(payload.secret, payload.code):
        raise HTTPException(status_code=400, detail="Neplatný ověřovací kód")
    user.totp_secret = payload.secret
    user.totp_enabled = True
    db.commit()
    return {"totp_enabled": True}


@app.post("/auth/2fa/disable")
def disable_2fa(
    payload: TwoFactorDisableRequest, username: str = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    user = db.get(AppUser, username)
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    valid_db_user = verify_password(payload.password, user.password_hash)
    if not valid_db_user and not authenticate(username, payload.password):
        raise HTTPException(status_code=401, detail="Nesprávné heslo")
    if user.totp_enabled and user.totp_secret and not verify_totp_code(user.totp_secret, payload.code):
        raise HTTPException(status_code=400, detail="Neplatný ověřovací kód")
    user.totp_secret = None
    user.totp_enabled = False
    db.commit()
    return {"totp_enabled": False}


@app.post("/users/{target_username}/2fa/reset")
def admin_reset_2fa(target_username: str, _: str = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Recovery path for a lost authenticator device - there is no email/SMS
    backup, so an admin has to be able to force 2FA back off for someone
    else's account. The user re-enrolls (setup + confirm) afterwards."""
    user = db.get(AppUser, target_username)
    if user is None:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    user.totp_secret = None
    user.totp_enabled = False
    db.commit()
    return {"username": target_username, "totp_enabled": False}


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
            "totp_enabled": False,
            "alert_daily_change_pct": None,
            "alert_drop_pct": None,
            "portfolios": user_portfolios(username, db),
        }
    return user_dict(user) | {"portfolios": user_portfolios(username, db)}


@app.put("/auth/me/notification-settings")
def update_notification_settings(
    payload: NotificationSettingsInput, username: str = Depends(require_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    user = db.get(AppUser, username)
    if user is None:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    for field, value in (("alert_daily_change_pct", payload.alert_daily_change_pct), ("alert_drop_pct", payload.alert_drop_pct)):
        if value is not None and not (Decimal("0.1") <= value <= Decimal("90")):
            raise HTTPException(status_code=400, detail="Práh musí být mezi 0,1 a 90 %")
    user.alert_daily_change_pct = payload.alert_daily_change_pct
    user.alert_drop_pct = payload.alert_drop_pct
    db.commit()
    return user_dict(user)


@app.get("/users")
def users(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(AppUser).order_by(AppUser.username)).all()
    # Embeds each user's Subjekt access (like /auth/me does for the caller
    # themselves) so the admin "Přístup uživatelů k subjektům" editor has
    # everything it needs from the one list call already made for this tab.
    return [user_dict(row) | {"portfolios": user_portfolios(row.username, db)} for row in rows]


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


@app.put("/users/{target_username}")
def update_user(
    target_username: str,
    payload: UserUpdateInput,
    acting_username: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Edits an existing user's display name, admin/active flags, global
    (rates/users/subjects) agendas, and optionally resets their password.
    Does not touch username (the primary key) or per-Subjekt access -
    that's PUT /users/{username}/portfolio-access."""
    user = db.get(AppUser, target_username)
    if user is None:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    if target_username == acting_username and (not payload.is_admin or not payload.is_active):
        raise HTTPException(status_code=400, detail="Nelze si sám sobě odebrat práva administrátora nebo se deaktivovat")
    allowed = [agenda for agenda in payload.allowed_agendas if agenda in ALL_AGENDAS]
    if payload.is_admin:
        allowed = ALL_AGENDAS
    user.full_name = payload.full_name
    user.is_admin = payload.is_admin
    user.is_active = payload.is_active
    user.allowed_agendas = allowed
    if payload.password:
        user.password_hash = hash_password(payload.password)
    db.commit()
    return user_dict(user) | {"portfolios": user_portfolios(user.username, db)}


@app.get("/portfolios")
def list_portfolios(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(Portfolio).order_by(Portfolio.name)).all()
    return [portfolio_dict(row) for row in rows]


@app.post("/portfolios")
def create_portfolio(payload: PortfolioInput, _: str = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Název subjektu je povinný")
    if db.scalar(select(Portfolio).where(Portfolio.name == name)) is not None:
        raise HTTPException(status_code=409, detail="Subjekt s tímto názvem už existuje")
    row = Portfolio(name=name)
    db.add(row)
    db.commit()
    return portfolio_dict(row)


@app.put("/portfolios/{portfolio_id}")
def rename_portfolio(
    portfolio_id: uuid.UUID, payload: PortfolioInput, _: str = Depends(require_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    row = db.get(Portfolio, portfolio_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Subjekt nenalezen")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Název subjektu je povinný")
    if db.scalar(select(Portfolio).where(Portfolio.name == name, Portfolio.id != portfolio_id)) is not None:
        raise HTTPException(status_code=409, detail="Subjekt s tímto názvem už existuje")
    row.name = name
    db.commit()
    return portfolio_dict(row)


def _portfolio_self_parties_dict(db: Session, portfolio_id: uuid.UUID) -> list[dict[str, Any]]:
    party_ids = {row.party_id for row in db.scalars(select(PortfolioSelfParty).where(PortfolioSelfParty.portfolio_id == portfolio_id)).all()}
    if not party_ids:
        return []
    parties = db.scalars(select(Party).where(Party.id.in_(party_ids)).order_by(Party.name)).all()
    return [{"id": str(p.id), "name": p.name} for p in parties]


@app.get("/portfolios/{portfolio_id}/self-parties")
def list_portfolio_self_parties(
    portfolio_id: uuid.UUID, _: str = Depends(require_admin), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Subjekt nenalezen")
    return _portfolio_self_parties_dict(db, portfolio_id)


@app.put("/portfolios/{portfolio_id}/self-parties")
def set_portfolio_self_parties(
    portfolio_id: uuid.UUID, payload: SelfPartiesInput, _: str = Depends(require_admin), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """Bulk-replaces which Party identities count as "us" for this Subjekt -
    used only by the Vyhodnocení report to classify loan interest as
    received/paid vs. excluded. See PortfolioSelfParty's docstring."""
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Subjekt nenalezen")
    valid_party_ids = {row.id for row in db.scalars(select(Party)).all()}
    db.execute(delete(PortfolioSelfParty).where(PortfolioSelfParty.portfolio_id == portfolio_id))
    for party_id in payload.party_ids:
        if party_id in valid_party_ids:
            db.add(PortfolioSelfParty(portfolio_id=portfolio_id, party_id=party_id))
    db.commit()
    return _portfolio_self_parties_dict(db, portfolio_id)


@app.put("/users/{target_username}/portfolio-access")
def set_user_portfolio_access(
    target_username: str, payload: PortfolioAccessInput, _: str = Depends(require_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Bulk-replaces a user's full set of Subjekt grants - same bulk-set (not
    patch/merge) style POST /users already uses for allowed_agendas."""
    if db.get(AppUser, target_username) is None:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    valid_portfolio_ids = {row.id for row in db.scalars(select(Portfolio)).all()}
    db.execute(delete(PortfolioAccess).where(PortfolioAccess.username == target_username))
    for grant in payload.grants:
        if grant.portfolio_id not in valid_portfolio_ids:
            continue
        allowed = [agenda for agenda in grant.allowed_agendas if agenda in PORTFOLIO_SCOPED_AGENDAS]
        if not allowed:
            continue
        db.add(PortfolioAccess(username=target_username, portfolio_id=grant.portfolio_id, allowed_agendas=allowed))
    db.commit()
    return {"username": target_username, "portfolios": user_portfolios(target_username, db)}


@app.get("/summary")
def summary(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access(None)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    latest_stat = db.scalar(
        select(DailyStatistic)
        .where(DailyStatistic.portfolio_id == portfolio_id)
        .order_by(desc(DailyStatistic.stat_date))
        .limit(1)
    )
    loan_total = db.scalar(select(func.coalesce(func.sum(LoanMovement.amount), 0)).where(LoanMovement.portfolio_id == portfolio_id))
    # A debt_interest-typed asset (Hypotéka) is money OWED, so its
    # borrowed_amount must reduce net worth rather than add to it - see
    # asset_net_worth_contribution. Portfolios here have a handful of assets,
    # so a Python loop is simpler (and keeps the branching logic in one
    # place) than duplicating the mode-dependent CASE in SQL.
    asset_types_by_id = {t.id: t.calculation_mode for t in db.scalars(select(AssetType).where(AssetType.portfolio_id == portfolio_id)).all()}
    asset_rows = db.scalars(select(Asset).where(Asset.portfolio_id == portfolio_id)).all()
    asset_total = sum(
        (asset_net_worth_contribution(row, asset_types_by_id.get(row.asset_type_id)) for row in asset_rows), Decimal("0")
    )
    cost_total = db.scalar(select(func.coalesce(func.sum(AssetCost.amount), 0)).where(AssetCost.portfolio_id == portfolio_id))
    portfolio_value = db.scalar(
        select(func.coalesce(func.sum(PortfolioPosition.market_value_czk), 0)).where(PortfolioPosition.portfolio_id == portfolio_id)
    )
    portfolio_profit = db.scalar(
        select(func.coalesce(func.sum(PortfolioPosition.profit_czk), 0)).where(PortfolioPosition.portfolio_id == portfolio_id)
    )
    return {
        "loans_total": json_value(loan_total),
        "assets_total": json_value(asset_total),
        "asset_costs_total": json_value(cost_total),
        "portfolio_value_czk": json_value(portfolio_value),
        "portfolio_profit_czk": json_value(portfolio_profit),
        "latest_stat": model_dict(latest_stat) if latest_stat else None,
        "counts": {
            "loan_movements": db.scalar(select(func.count()).select_from(LoanMovement).where(LoanMovement.portfolio_id == portfolio_id)),
            "assets": db.scalar(select(func.count()).select_from(Asset).where(Asset.portfolio_id == portfolio_id)),
            "asset_costs": db.scalar(select(func.count()).select_from(AssetCost).where(AssetCost.portfolio_id == portfolio_id)),
            "stock_transactions": db.scalar(
                select(func.count()).select_from(StockTransaction).where(StockTransaction.portfolio_id == portfolio_id)
            ),
            "watchlist_stocks": db.scalar(
                select(func.count()).select_from(WatchlistStock).where(WatchlistStock.portfolio_id == portfolio_id)
            ),
            "portfolio_positions": db.scalar(
                select(func.count()).select_from(PortfolioPosition).where(PortfolioPosition.portfolio_id == portfolio_id)
            ),
            "daily_statistics": db.scalar(
                select(func.count()).select_from(DailyStatistic).where(DailyStatistic.portfolio_id == portfolio_id)
            ),
            "exchange_rates": db.scalar(select(func.count()).select_from(ExchangeRate)),
        },
    }


@app.post("/evaluations/compute")
def compute_evaluation(
    period: str | None = Query(None, description='"YYYY-MM", defaults to the current month'),
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("evaluations")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    period = period or date.today().strftime("%Y-%m")
    evaluation = compute_monthly_evaluation(db, portfolio_id, period)
    return _evaluation_dict(db, evaluation)


def _evaluation_dict(db: Session, evaluation: MonthlyEvaluation) -> dict[str, Any]:
    assets_by_id = {a.id: a for a in db.scalars(select(Asset).where(Asset.portfolio_id == evaluation.portfolio_id)).all()}
    cashflows = db.scalars(
        select(MonthlyEvaluationAssetCashflow).where(MonthlyEvaluationAssetCashflow.evaluation_id == evaluation.id)
    ).all()
    latest_stat_date = db.scalar(
        select(func.max(DailyStatistic.stat_date)).where(DailyStatistic.portfolio_id == evaluation.portfolio_id)
    )
    return model_dict(evaluation) | {
        "stock_data_as_of": latest_stat_date.isoformat() if latest_stat_date else None,
        "asset_cashflows": [
            {
                "asset_id": str(row.asset_id) if row.asset_id else None,
                "asset_code": assets_by_id[row.asset_id].code if row.asset_id in assets_by_id else None,
                "asset_name": assets_by_id[row.asset_id].name if row.asset_id in assets_by_id else "Bez majetku",
                "income_czk": json_value(row.income_czk),
                "expense_czk": json_value(row.expense_czk),
            }
            for row in cashflows
        ],
    }


@app.get("/evaluations")
def list_evaluations(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("evaluations")), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    evaluations = db.scalars(
        select(MonthlyEvaluation).where(MonthlyEvaluation.portfolio_id == portfolio_id).order_by(desc(MonthlyEvaluation.period))
    ).all()
    return [_evaluation_dict(db, evaluation) for evaluation in evaluations]


@app.get("/evaluations/{evaluation_id}/interest-detail")
def evaluation_interest_detail(
    evaluation_id: uuid.UUID,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("evaluations")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Per-movement/per-Hypotéka breakdown of interest_received_czk/
    interest_paid_czk - recomputed live from classify_loan_interest, not
    stored (the aggregate on MonthlyEvaluation is the only persisted figure;
    this just re-derives the same numbers' composition on demand)."""
    evaluation = db.scalar(
        select(MonthlyEvaluation).where(MonthlyEvaluation.id == evaluation_id, MonthlyEvaluation.portfolio_id == portfolio_id)
    )
    if evaluation is None:
        raise HTTPException(status_code=404, detail="Vyhodnocení nenalezeno")
    self_party_ids = {
        row.party_id for row in db.scalars(select(PortfolioSelfParty).where(PortfolioSelfParty.portfolio_id == portfolio_id)).all()
    }
    _, _, detail = classify_loan_interest(db, portfolio_id, self_party_ids, evaluation.period)
    return {
        "received": [row for row in detail if row["direction"] == "received"],
        "paid": [row for row in detail if row["direction"] == "paid"],
    }


@app.get("/loans/movements")
def loan_movements(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("loans")), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(LoanMovement)
        .where(LoanMovement.portfolio_id == portfolio_id)
        .order_by(desc(LoanMovement.movement_date).nullslast())
        .limit(500)
    ).all()
    party_names = {p.id: p.name for p in db.scalars(select(Party)).all()}
    result = []
    for row in rows:
        data = model_dict(row)
        data["lender"] = party_names.get(row.lender_id)
        data["borrower"] = party_names.get(row.borrower_id)
        data["computed_interest_plan"] = loan_movement_interest_plan(row)
        result.append(data)
    return result


def _loan_movement_dict(db: Session, row: LoanMovement) -> dict[str, Any]:
    lender = db.get(Party, row.lender_id) if row.lender_id else None
    borrower = db.get(Party, row.borrower_id) if row.borrower_id else None
    return model_dict(row) | {
        "lender": lender.name if lender else None,
        "borrower": borrower.name if borrower else None,
        "computed_interest_plan": loan_movement_interest_plan(row),
    }


@app.post("/loans/movements")
def create_loan_movement(
    payload: LoanMovementInput,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("loans")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    lender_name = payload.lender.strip()
    borrower_name = payload.borrower.strip()
    if not lender_name or not borrower_name:
        raise HTTPException(status_code=400, detail="Věřitel a dlužník jsou povinní")
    lender = get_or_create_party(db, lender_name, "lender")
    borrower = get_or_create_party(db, borrower_name, "borrower")
    row = LoanMovement(
        portfolio_id=portfolio_id,
        movement_date=payload.movement_date,
        period_label=(payload.period_label or "").strip() or None,
        lender_id=lender.id,
        borrower_id=borrower.id,
        amount=payload.amount,
        interest_rate=payload.interest_rate,
        interest_period=(payload.interest_period or "").strip() or None,
        planned_end_date=payload.planned_end_date,
        completed_at=payload.completed_at,
        description=(payload.description or "").strip() or None,
    )
    db.add(row)
    db.commit()
    return _loan_movement_dict(db, row)


@app.put("/loans/movements/{movement_id}")
def update_loan_movement(
    movement_id: uuid.UUID,
    payload: LoanMovementInput,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("loans")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(LoanMovement).where(LoanMovement.id == movement_id, LoanMovement.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Pohyb nenalezen")
    lender_name = payload.lender.strip()
    borrower_name = payload.borrower.strip()
    if not lender_name or not borrower_name:
        raise HTTPException(status_code=400, detail="Věřitel a dlužník jsou povinní")
    lender = get_or_create_party(db, lender_name, "lender")
    borrower = get_or_create_party(db, borrower_name, "borrower")
    row.movement_date = payload.movement_date
    row.period_label = (payload.period_label or "").strip() or None
    row.lender_id = lender.id
    row.borrower_id = borrower.id
    row.amount = payload.amount
    row.interest_rate = payload.interest_rate
    row.interest_period = (payload.interest_period or "").strip() or None
    row.planned_end_date = payload.planned_end_date
    row.completed_at = payload.completed_at
    row.description = (payload.description or "").strip() or None
    db.commit()
    return _loan_movement_dict(db, row)


@app.delete("/loans/movements/{movement_id}")
def delete_loan_movement(
    movement_id: uuid.UUID,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("loans")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(LoanMovement).where(LoanMovement.id == movement_id, LoanMovement.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Pohyb nenalezen")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@app.get("/loans/balances")
def loan_balances(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("loans")), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Current state of who owes whom: real data confirms a movement's
    signed amount is positive when the lender gives the borrower money and
    negative for a repayment/return (e.g. "Vrácení příplatku k vlastnímu
    kapitálu") - so summing amount within a (lender, borrower) pair yields
    the net principal still outstanding on that specific relationship.
    Pairs with both directions between the same two parties (A lent to B
    AND B separately lent to A) are kept as two separate rows rather than
    netted together, since they represent separate movements/agreements.
    """
    rows = db.execute(
        select(
            LoanMovement.lender_id,
            LoanMovement.borrower_id,
            func.sum(LoanMovement.amount).label("net_amount"),
            func.count().label("movement_count"),
            func.max(LoanMovement.movement_date).label("latest_movement_date"),
        )
        .where(LoanMovement.portfolio_id == portfolio_id)
        .group_by(LoanMovement.lender_id, LoanMovement.borrower_id)
    ).all()
    party_names = {p.id: p.name for p in db.scalars(select(Party)).all()}
    balances = [
        {
            "lender_id": str(row.lender_id) if row.lender_id else None,
            "lender": party_names.get(row.lender_id),
            "borrower_id": str(row.borrower_id) if row.borrower_id else None,
            "borrower": party_names.get(row.borrower_id),
            "net_amount": json_value(row.net_amount),
            "movement_count": row.movement_count,
            "latest_movement_date": row.latest_movement_date.isoformat() if row.latest_movement_date else None,
        }
        for row in rows
    ]
    balances.sort(key=lambda b: abs(b["net_amount"] or 0), reverse=True)
    total_outstanding = round(sum(b["net_amount"] for b in balances if b["net_amount"] and b["net_amount"] > 0), 2)
    return {"balances": balances, "total_outstanding": total_outstanding}


@app.get("/loans/movements/{movement_id}/interest-projection")
def loan_movement_interest_projection(
    movement_id: uuid.UUID,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("loans")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    movement = db.scalar(select(LoanMovement).where(LoanMovement.id == movement_id, LoanMovement.portfolio_id == portfolio_id))
    if movement is None:
        raise HTTPException(status_code=404, detail="Pohyb nenalezen")
    projection = loan_movement_interest_plan(movement)
    return {
        "movement_id": str(movement.id),
        "computed_plan": projection,
        "total_computed": round(sum(projection.values()), 2) if projection else 0,
    }


@app.get("/loans/movements/{movement_id}/payment-schedule")
def loan_movement_payment_schedule(
    movement_id: uuid.UUID,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("loans")),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    movement = db.scalar(select(LoanMovement).where(LoanMovement.id == movement_id, LoanMovement.portfolio_id == portfolio_id))
    if movement is None:
        raise HTTPException(status_code=404, detail="Pohyb nenalezen")
    schedule = amortization_schedule(
        pv=movement.amount,
        interest_rate=movement.interest_rate,
        start_date=movement.movement_date,
        end_date=movement.planned_end_date,
    )
    return [
        {
            "period": period["period"],
            "date": period["date"].isoformat(),
            "payment": json_value(period["payment"]),
            "principal": json_value(period["principal"]),
            "interest": json_value(period["interest"]),
            "balance": json_value(period["balance"]),
        }
        for period in schedule
    ]


@app.post("/loans/cleanup-imported-subtotals")
def cleanup_loan_subtotals(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("loans")), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """One-off cleanup for databases imported before the fix: the source
    "Půjčky Pohyby" sheet has monthly/yearly subtotal rows baked directly
    into the data (a "Leden 2023"/"2023" text label instead of a real date,
    no lender/borrower) which used to be imported as if they were real loan
    movements. import_loans no longer creates these, but a database imported
    before that fix still has the old ones sitting in it - this deletes just
    those (movement_date IS NULL, never true for a real movement) without
    touching any real loan data or requiring a full destructive re-import.
    """
    deleted = db.execute(
        delete(LoanMovement).where(LoanMovement.portfolio_id == portfolio_id, LoanMovement.movement_date.is_(None))
    ).rowcount
    db.commit()
    return {"deleted": deleted}


def _asset_dict(db: Session, row: Asset) -> dict[str, Any]:
    owner = db.get(Party, row.owner_id) if row.owner_id else None
    asset_type = db.get(AssetType, row.asset_type_id) if row.asset_type_id else None
    linked = db.get(Asset, row.linked_asset_id) if row.linked_asset_id else None
    calculation_mode = asset_type.calculation_mode if asset_type else None
    return model_dict(row) | {
        "owner": owner.name if owner else None,
        "asset_type": asset_type.name if asset_type else row.asset_type,
        "calculation_mode": calculation_mode,
        "computed_interest_plan": computed_interest_plan(row, calculation_mode),
        "net_worth_contribution": json_value(asset_net_worth_contribution(row, calculation_mode)),
        "linked_asset": linked.name if linked else None,
        "linked_asset_code": linked.code if linked else None,
    }


def _validate_asset_type_id(db: Session, portfolio_id: uuid.UUID, asset_type_id: uuid.UUID | None) -> AssetType | None:
    if asset_type_id is None:
        return None
    row = db.scalar(select(AssetType).where(AssetType.id == asset_type_id, AssetType.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Typ majetku nenalezen")
    return row


def _validate_linked_asset_id(
    db: Session, portfolio_id: uuid.UUID, linked_asset_id: uuid.UUID | None, exclude_id: uuid.UUID | None
) -> None:
    if linked_asset_id is None:
        return
    if linked_asset_id == exclude_id:
        raise HTTPException(status_code=400, detail="Majetek nemůže být navázán sám na sebe")
    if db.scalar(select(Asset).where(Asset.id == linked_asset_id, Asset.portfolio_id == portfolio_id)) is None:
        raise HTTPException(status_code=404, detail="Navázaný majetek nenalezen")


@app.get("/assets")
def assets(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("assets")), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    owners = {p.id: p.name for p in db.scalars(select(Party)).all()}
    asset_types = {t.id: t for t in db.scalars(select(AssetType).where(AssetType.portfolio_id == portfolio_id)).all()}
    rows = db.scalars(select(Asset).where(Asset.portfolio_id == portfolio_id).order_by(Asset.code)).all()
    assets_by_id = {row.id: row for row in rows}
    result = []
    for row in rows:
        asset_type = asset_types.get(row.asset_type_id)
        calculation_mode = asset_type.calculation_mode if asset_type else None
        linked = assets_by_id.get(row.linked_asset_id) if row.linked_asset_id else None
        result.append(
            model_dict(row)
            | {
                "owner": owners.get(row.owner_id),
                "asset_type": asset_type.name if asset_type else row.asset_type,
                "calculation_mode": calculation_mode,
                "computed_interest_plan": computed_interest_plan(row, calculation_mode),
                "net_worth_contribution": json_value(asset_net_worth_contribution(row, calculation_mode)),
                "linked_asset": linked.name if linked else None,
                "linked_asset_code": linked.code if linked else None,
            }
        )
    return result


@app.post("/assets")
def create_asset(
    payload: AssetInput,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("assets")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    code = payload.code.strip()
    name = payload.name.strip()
    if not code or not name:
        raise HTTPException(status_code=400, detail="Kód a název jsou povinné")
    if db.scalar(select(Asset).where(Asset.portfolio_id == portfolio_id, Asset.code == code)) is not None:
        raise HTTPException(status_code=409, detail="Majetek s tímto kódem už existuje")
    owner = get_or_create_party(db, (payload.owner or "").strip() or None, "owner")
    _validate_asset_type_id(db, portfolio_id, payload.asset_type_id)
    _validate_linked_asset_id(db, portfolio_id, payload.linked_asset_id, None)
    row = Asset(
        portfolio_id=portfolio_id,
        code=code,
        name=name,
        owner_id=owner.id if owner else None,
        asset_type_id=payload.asset_type_id,
        linked_asset_id=payload.linked_asset_id,
        total_value=payload.total_value,
        own_funds=payload.own_funds,
        borrowed_amount=payload.borrowed_amount,
        lender_name=payload.lender_name,
        borrowed_from=payload.borrowed_from,
        borrowed_to=payload.borrowed_to,
        interest_rate=payload.interest_rate,
        loan_years=payload.loan_years,
        fixed_until=payload.fixed_until,
        payment=payload.payment,
    )
    db.add(row)
    db.commit()
    return _asset_dict(db, row)


@app.put("/assets/{asset_id}")
def update_asset(
    asset_id: uuid.UUID,
    payload: AssetInput,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("assets")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(Asset).where(Asset.id == asset_id, Asset.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Majetek nenalezen")
    code = payload.code.strip()
    name = payload.name.strip()
    if not code or not name:
        raise HTTPException(status_code=400, detail="Kód a název jsou povinné")
    if db.scalar(
        select(Asset).where(Asset.portfolio_id == portfolio_id, Asset.code == code, Asset.id != asset_id)
    ) is not None:
        raise HTTPException(status_code=409, detail="Majetek s tímto kódem už existuje")
    owner = get_or_create_party(db, (payload.owner or "").strip() or None, "owner")
    _validate_asset_type_id(db, portfolio_id, payload.asset_type_id)
    _validate_linked_asset_id(db, portfolio_id, payload.linked_asset_id, asset_id)
    row.code = code
    row.name = name
    row.owner_id = owner.id if owner else None
    row.asset_type_id = payload.asset_type_id
    row.linked_asset_id = payload.linked_asset_id
    row.total_value = payload.total_value
    row.own_funds = payload.own_funds
    row.borrowed_amount = payload.borrowed_amount
    row.lender_name = payload.lender_name
    row.borrowed_from = payload.borrowed_from
    row.borrowed_to = payload.borrowed_to
    row.interest_rate = payload.interest_rate
    row.loan_years = payload.loan_years
    row.fixed_until = payload.fixed_until
    row.payment = payload.payment
    db.commit()
    return _asset_dict(db, row)


@app.delete("/assets/{asset_id}")
def delete_asset(
    asset_id: uuid.UUID,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("assets")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(Asset).where(Asset.id == asset_id, Asset.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Majetek nenalezen")
    if db.scalar(select(func.count()).select_from(AssetCost).where(AssetCost.asset_id == asset_id)) > 0:
        raise HTTPException(status_code=409, detail="Nelze smazat majetek, ke kterému jsou navázány náklady")
    if db.scalar(select(func.count()).select_from(Asset).where(Asset.linked_asset_id == asset_id)) > 0:
        raise HTTPException(
            status_code=409, detail="Nelze smazat majetek, na který je navázaná hypotéka - nejprve ji smažte nebo odpojte"
        )
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@app.get("/assets/{asset_id}/interest-projection")
def asset_interest_projection(
    asset_id: str,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("assets")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    asset = db.scalar(select(Asset).where(Asset.id == asset_id, Asset.portfolio_id == portfolio_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Majetek nenalezen")
    asset_type = db.get(AssetType, asset.asset_type_id) if asset.asset_type_id else None
    projection = computed_interest_plan(asset, asset_type.calculation_mode if asset_type else None)
    return {
        "asset_id": str(asset.id),
        "asset_code": asset.code,
        "imported_plan": asset.annual_interest_plan or {},
        "computed_plan": projection,
        "total_computed": round(sum(projection.values()), 2) if projection else 0,
    }


@app.get("/assets/{asset_id}/payment-schedule")
def asset_payment_schedule(
    asset_id: str,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("assets")),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    asset = db.scalar(select(Asset).where(Asset.id == asset_id, Asset.portfolio_id == portfolio_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Majetek nenalezen")
    schedule = amortization_schedule(
        pv=asset.borrowed_amount,
        interest_rate=asset.interest_rate,
        start_date=asset.borrowed_from,
        loan_years=asset.loan_years,
        end_date=asset.borrowed_to,
    )
    return [
        {
            "period": period["period"],
            "date": period["date"].isoformat(),
            "payment": json_value(period["payment"]),
            "principal": json_value(period["principal"]),
            "interest": json_value(period["interest"]),
            "balance": json_value(period["balance"]),
        }
        for period in schedule
    ]


@app.get("/assets/asset-types")
def list_asset_types(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("asset_types")), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.scalars(select(AssetType).where(AssetType.portfolio_id == portfolio_id).order_by(AssetType.name)).all()
    return [
        {"id": str(row.id), "name": row.name, "calculation_mode": row.calculation_mode, "required_fields": row.required_fields or []}
        for row in rows
    ]


@app.post("/assets/asset-types")
def create_asset_type(
    payload: AssetTypeInput,
    portfolio_id: uuid.UUID = Query(...),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Subjekt nenalezen")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Název typu je povinný")
    if db.scalar(select(AssetType).where(AssetType.portfolio_id == portfolio_id, AssetType.name == name)) is not None:
        raise HTTPException(status_code=409, detail="Typ s tímto názvem už existuje")
    invalid_fields = set(payload.required_fields) - set(ASSET_REQUIRED_FIELD_CHOICES)
    if invalid_fields:
        raise HTTPException(status_code=400, detail=f"Neznámá pole: {', '.join(sorted(invalid_fields))}")
    row = AssetType(portfolio_id=portfolio_id, name=name, calculation_mode=payload.calculation_mode, required_fields=payload.required_fields)
    db.add(row)
    db.commit()
    return {"id": str(row.id), "name": row.name, "calculation_mode": row.calculation_mode, "required_fields": row.required_fields or []}


@app.put("/assets/asset-types/{type_id}")
def update_asset_type(
    type_id: uuid.UUID,
    payload: AssetTypeInput,
    portfolio_id: uuid.UUID = Query(...),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(AssetType).where(AssetType.id == type_id, AssetType.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Typ majetku nenalezen")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Název typu je povinný")
    if db.scalar(
        select(AssetType).where(AssetType.portfolio_id == portfolio_id, AssetType.name == name, AssetType.id != type_id)
    ) is not None:
        raise HTTPException(status_code=409, detail="Typ s tímto názvem už existuje")
    invalid_fields = set(payload.required_fields) - set(ASSET_REQUIRED_FIELD_CHOICES)
    if invalid_fields:
        raise HTTPException(status_code=400, detail=f"Neznámá pole: {', '.join(sorted(invalid_fields))}")
    row.name = name
    row.calculation_mode = payload.calculation_mode
    row.required_fields = payload.required_fields
    db.commit()
    return {"id": str(row.id), "name": row.name, "calculation_mode": row.calculation_mode, "required_fields": row.required_fields or []}


@app.delete("/assets/asset-types/{type_id}")
def delete_asset_type(type_id: uuid.UUID, _: str = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(AssetType, type_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Typ majetku nenalezen")
    if db.scalar(select(func.count()).select_from(Asset).where(Asset.asset_type_id == type_id)) > 0:
        raise HTTPException(status_code=409, detail="Typ je použit u majetku, nelze jej smazat")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@app.get("/parties")
def list_parties(_: str = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Every Party regardless of kind - admin-only, used by the Subjekty
    tab's "Vlastní jména" picker (PortfolioSelfParty can reference any kind,
    e.g. an owner-kind Party as well as a lender/borrower-kind one)."""
    rows = db.scalars(select(Party).order_by(Party.name)).all()
    return [{"id": str(row.id), "name": row.name, "kind": row.kind} for row in rows]


@app.get("/parties/payers")
def list_payers(_: str = Depends(require_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(Party).where(Party.kind == "payer").order_by(Party.name)).all()
    return [{"id": str(row.id), "name": row.name} for row in rows]


@app.post("/parties/payers")
def create_payer(payload: PayerInput, _: str = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Jméno plátce je povinné")
    existing = db.scalar(select(Party).where(Party.name == name))
    if existing is not None:
        if existing.kind == "payer":
            raise HTTPException(status_code=409, detail="Plátce s tímto jménem už existuje")
        if existing.kind != "unknown":
            raise HTTPException(status_code=409, detail=f"Jméno „{name}“ už existuje jako jiný typ záznamu, nelze jej použít pro plátce")
        existing.kind = "payer"
        db.commit()
        return {"id": str(existing.id), "name": existing.name}
    row = Party(name=name, kind="payer")
    db.add(row)
    db.commit()
    return {"id": str(row.id), "name": row.name}


@app.delete("/parties/payers/{payer_id}")
def delete_payer(payer_id: uuid.UUID, _: str = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(Party, payer_id)
    if row is None or row.kind != "payer":
        raise HTTPException(status_code=404, detail="Plátce nenalezen")
    if db.scalar(select(func.count()).select_from(AssetCost).where(AssetCost.payer_id == payer_id)) > 0:
        raise HTTPException(status_code=409, detail="Plátce je použit u nákladu, nelze jej smazat")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@app.get("/assets/costs")
def asset_costs(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("costs")), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(AssetCost)
        .where(AssetCost.portfolio_id == portfolio_id)
        .order_by(desc(AssetCost.cost_date).nullslast())
        .limit(500)
    ).all()
    assets_by_id = {a.id: a.name for a in db.scalars(select(Asset)).all()}
    payers = {p.id: p.name for p in db.scalars(select(Party)).all()}
    categories_by_id = {
        c.id: c.name for c in db.scalars(select(CostCategory).where(CostCategory.portfolio_id == portfolio_id)).all()
    }
    return [
        model_dict(row)
        | {
            "asset": assets_by_id.get(row.asset_id),
            "payer": payers.get(row.payer_id),
            # Dictionary-backed name wins; legacy free-text is only a
            # fallback for the rare pre-migration row that ensure_schema_
            # upgrades() hasn't backfilled a category_id for yet.
            "category": categories_by_id.get(row.category_id) or row.category,
            "has_attachment": cost_attachment_path(row.id).exists(),
        }
        for row in rows
    ]


def _cost_dict(db: Session, row: AssetCost) -> dict[str, Any]:
    asset = db.get(Asset, row.asset_id) if row.asset_id else None
    payer = db.get(Party, row.payer_id) if row.payer_id else None
    category = db.get(CostCategory, row.category_id) if row.category_id else None
    return model_dict(row) | {
        "asset": asset.name if asset else None,
        "payer": payer.name if payer else None,
        "category": category.name if category else row.category,
        "has_attachment": cost_attachment_path(row.id).exists(),
    }


@app.post("/assets/costs")
def create_asset_cost(
    payload: AssetCostInput,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("costs")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if payload.asset_id is not None and db.scalar(
        select(Asset).where(Asset.id == payload.asset_id, Asset.portfolio_id == portfolio_id)
    ) is None:
        raise HTTPException(status_code=404, detail="Majetek nenalezen")
    item = payload.item.strip()
    if not item:
        raise HTTPException(status_code=400, detail="Položka je povinná")
    category = get_or_create_cost_category(db, portfolio_id, (payload.category or "").strip() or None)
    payer = get_or_create_party(db, (payload.payer or "").strip() or None, "payer")
    row = AssetCost(
        portfolio_id=portfolio_id,
        asset_id=payload.asset_id,
        cost_date=payload.cost_date,
        item=item,
        category_id=category.id if category else None,
        amount=payload.amount,
        supplier=(payload.supplier or "").strip() or None,
        payer_id=payer.id if payer else None,
        note=(payload.note or "").strip() or None,
    )
    db.add(row)
    db.commit()
    return _cost_dict(db, row)


@app.put("/assets/costs/{cost_id}")
def update_asset_cost(
    cost_id: uuid.UUID,
    payload: AssetCostInput,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("costs")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(AssetCost).where(AssetCost.id == cost_id, AssetCost.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Náklad nenalezen")
    if payload.asset_id is not None and db.scalar(
        select(Asset).where(Asset.id == payload.asset_id, Asset.portfolio_id == portfolio_id)
    ) is None:
        raise HTTPException(status_code=404, detail="Majetek nenalezen")
    item = payload.item.strip()
    if not item:
        raise HTTPException(status_code=400, detail="Položka je povinná")
    category = get_or_create_cost_category(db, portfolio_id, (payload.category or "").strip() or None)
    payer = get_or_create_party(db, (payload.payer or "").strip() or None, "payer")
    row.asset_id = payload.asset_id
    row.cost_date = payload.cost_date
    row.item = item
    row.category_id = category.id if category else None
    row.amount = payload.amount
    row.supplier = (payload.supplier or "").strip() or None
    row.payer_id = payer.id if payer else None
    row.note = (payload.note or "").strip() or None
    db.commit()
    return _cost_dict(db, row)


@app.delete("/assets/costs/{cost_id}")
def delete_asset_cost(
    cost_id: uuid.UUID,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("costs")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(AssetCost).where(AssetCost.id == cost_id, AssetCost.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Náklad nenalezen")
    attachment = cost_attachment_path(cost_id)
    if attachment.exists():
        attachment.unlink()
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


PDF_MAGIC_BYTES = b"%PDF-"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


@app.post("/assets/costs/{cost_id}/attachment")
async def upload_cost_attachment(
    cost_id: uuid.UUID,
    file: UploadFile = File(...),
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("costs")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(AssetCost).where(AssetCost.id == cost_id, AssetCost.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Náklad nenalezen")
    content = await file.read()
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=400, detail="Příloha je příliš velká (max 20 MB)")
    # Content-Type is client-supplied and easy to spoof - the magic-bytes
    # check is what actually keeps this from writing arbitrary uploads to
    # disk under a .pdf-shaped path.
    if not content.startswith(PDF_MAGIC_BYTES):
        raise HTTPException(status_code=400, detail="Přílohou může být jen platný PDF soubor")
    cost_attachment_path(cost_id).write_bytes(content)
    return {"status": "uploaded"}


@app.get("/assets/costs/{cost_id}/attachment")
def download_cost_attachment(
    cost_id: uuid.UUID,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("costs")),
    db: Session = Depends(get_db),
) -> FileResponse:
    row = db.scalar(select(AssetCost).where(AssetCost.id == cost_id, AssetCost.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Náklad nenalezen")
    path = cost_attachment_path(cost_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Příloha nenalezena")
    return FileResponse(path, media_type="application/pdf", filename=f"{cost_id}.pdf")


@app.delete("/assets/costs/{cost_id}/attachment")
def delete_cost_attachment(
    cost_id: uuid.UUID,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("costs")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.scalar(select(AssetCost).where(AssetCost.id == cost_id, AssetCost.portfolio_id == portfolio_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Náklad nenalezen")
    path = cost_attachment_path(cost_id)
    if path.exists():
        path.unlink()
    return {"status": "deleted"}


@app.get("/assets/cost-categories")
def list_cost_categories(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("categories")), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(CostCategory).where(CostCategory.portfolio_id == portfolio_id).order_by(CostCategory.name)
    ).all()
    return [{"id": str(row.id), "name": row.name} for row in rows]


@app.post("/assets/cost-categories")
def create_cost_category(
    payload: CostCategoryInput,
    portfolio_id: uuid.UUID = Query(...),
    _: str = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Subjekt nenalezen")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Název kategorie je povinný")
    if db.scalar(select(CostCategory).where(CostCategory.portfolio_id == portfolio_id, CostCategory.name == name)) is not None:
        raise HTTPException(status_code=409, detail="Kategorie s tímto názvem už existuje")
    row = CostCategory(portfolio_id=portfolio_id, name=name)
    db.add(row)
    db.commit()
    return {"id": str(row.id), "name": row.name}


@app.delete("/assets/cost-categories/{category_id}")
def delete_cost_category(
    category_id: uuid.UUID, _: str = Depends(require_admin), db: Session = Depends(get_db)
) -> dict[str, Any]:
    row = db.get(CostCategory, category_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Kategorie nenalezena")
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@app.get("/stocks/transactions")
def stock_transactions(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access(("transactions", "history"))),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(StockTransaction)
        .where(StockTransaction.portfolio_id == portfolio_id)
        .order_by(desc(StockTransaction.traded_on).nullslast())
        .limit(500)
    ).all()
    return [model_dict(row) for row in rows]


@app.get("/stocks/portfolio")
def portfolio(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("portfolio")), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(PortfolioPosition)
        .where(PortfolioPosition.portfolio_id == portfolio_id)
        .order_by(desc(PortfolioPosition.market_value_czk).nullslast())
    ).all()
    return [model_dict(row) for row in rows]


@app.get("/stocks/watchlist")
def stock_watchlist(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("watchlist")), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(WatchlistStock)
        .where(WatchlistStock.portfolio_id == portfolio_id)
        .order_by(desc(WatchlistStock.watched_on).nullslast(), WatchlistStock.ticker)
    ).all()
    return [model_dict(row) for row in rows]


@app.get("/stocks/statistics")
def stock_statistics(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access(("stats", "charts"))), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(DailyStatistic).where(DailyStatistic.portfolio_id == portfolio_id).order_by(desc(DailyStatistic.stat_date)).limit(200)
    ).all()
    return [model_dict(row) for row in rows]


@app.get("/stocks/overview")
def stock_overview(
    portfolio_id: uuid.UUID = Depends(require_portfolio_access(("transactions", "watchlist", "portfolio"))),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    movements = db.execute(
        select(
            StockTransaction.movement_type,
            func.count().label("count"),
            func.coalesce(func.sum(StockTransaction.amount_czk), 0).label("amount_czk"),
        )
        .where(StockTransaction.portfolio_id == portfolio_id)
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
        .where(StockTransaction.portfolio_id == portfolio_id)
        .group_by(StockTransaction.currency)
        .order_by(StockTransaction.currency)
    ).all()
    top_profit = db.scalars(
        select(PortfolioPosition)
        .where(PortfolioPosition.portfolio_id == portfolio_id)
        .order_by(desc(PortfolioPosition.profit_czk).nullslast())
        .limit(8)
    ).all()
    top_loss = db.scalars(
        select(PortfolioPosition)
        .where(PortfolioPosition.portfolio_id == portfolio_id)
        .order_by(PortfolioPosition.profit_czk.nullslast())
        .limit(8)
    ).all()
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
    username: str = Depends(require_user),
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("alerts")),
    db: Session = Depends(get_db),
    threshold_pct: Decimal | None = Query(default=None),
) -> dict[str, Any]:
    return compute_alerts(db, portfolio_id, threshold_pct=resolve_threshold(db, username, threshold_pct, "alert_drop_pct"))


@app.get("/stocks/ticker-history")
def stock_ticker_history(
    ticker: str,
    date_from: date,
    date_to: date,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("history")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return build_ticker_history(db, portfolio_id, ticker=ticker, date_from=date_from, date_to=date_to)


@app.post("/stocks/import-patria")
def import_patria(
    payload: PatriaImportInput,
    portfolio_id: uuid.UUID = Depends(require_portfolio_access("transactions")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Text z Patrie je prázdný")
    return import_patria_trades(db, portfolio_id, payload.text)


@app.post("/stocks/refresh-prices")
def refresh_stock_prices(
    username: str = Depends(require_user),
    portfolio_id: uuid.UUID = Depends(require_portfolio_access(None)),
    db: Session = Depends(get_db),
    threshold_pct: Decimal | None = Query(default=None),
) -> dict[str, Any]:
    return refresh_current_prices(
        db, portfolio_id, threshold_pct=resolve_threshold(db, username, threshold_pct, "alert_daily_change_pct")
    )


@app.post("/stocks/recalculate")
def recalculate_stock_data(
    username: str = Depends(require_user),
    portfolio_id: uuid.UUID = Depends(require_portfolio_access(None)),
    db: Session = Depends(get_db),
    dry_run: bool = False,
    date_from: date | None = Query(default=None),
    threshold_pct: Decimal | None = Query(default=None),
) -> dict[str, Any]:
    # An unhandled exception here used to reach the browser as a bare 500
    # with no CORS headers (Starlette's outermost error handler sits above
    # CORSMiddleware) - the browser then reports it as a CORS failure
    # ("Failed to fetch") with no hint of the real cause. Catch broadly and
    # re-raise as a normal HTTPException instead, which *does* get CORS
    # headers and puts the actual error text in front of the user/logs.
    try:
        effective_threshold = resolve_threshold(db, username, threshold_pct, "alert_daily_change_pct")
        # Auto-fetch missing CNB rates first, same as AktualizujStatistiku always
        # does before recomputing - but only for a real (non-preview) run, so
        # "Kontrola (náhled)" keeps its "nothing gets saved" promise.
        cnb_rates_added = ensure_cnb_rates_up_to_date(db) if not dry_run else 0
        result = recalculate_stocks(db, portfolio_id, dry_run=dry_run, date_from=date_from, threshold_pct=effective_threshold)
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
    portfolio_id: uuid.UUID = Depends(require_portfolio_access(None)),
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
    counts = import_workbooks(finance_path, property_path, portfolio_id=portfolio_id)
    return {"status": "done", "counts": counts}
