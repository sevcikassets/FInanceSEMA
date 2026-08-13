import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_file: Mapped[str] = mapped_column(Text)
    source_checksum: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="running")
    row_counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Party(Base):
    __tablename__ = "parties"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppUser(Base):
    __tablename__ = "app_users"

    username: Mapped[str] = mapped_column(String(128), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    is_admin: Mapped[bool] = mapped_column(default=False)
    allowed_agendas: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoanMovement(Base):
    __tablename__ = "loan_movements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    movement_date: Mapped[date | None] = mapped_column(Date)
    period_label: Mapped[str | None] = mapped_column(String(64))
    lender_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("parties.id"))
    borrower_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("parties.id"))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    interest_period: Mapped[str | None] = mapped_column(String(64))
    planned_end_date: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(Text)
    source_row: Mapped[int | None]

    lender: Mapped[Party | None] = relationship(foreign_keys=[lender_id])
    borrower: Mapped[Party | None] = relationship(foreign_keys=[borrower_id])


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("parties.id"))
    asset_type: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    total_value: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    own_funds: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    borrowed_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    lender_name: Mapped[str | None] = mapped_column(String(255))
    borrowed_from: Mapped[date | None] = mapped_column(Date)
    borrowed_to: Mapped[date | None] = mapped_column(Date)
    interest_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    loan_years: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    fixed_until: Mapped[date | None] = mapped_column(Date)
    payment: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    annual_interest_plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_row: Mapped[int | None]

    owner: Mapped[Party | None] = relationship()


class AssetCost(Base):
    __tablename__ = "asset_costs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("assets.id"))
    cost_date: Mapped[date | None] = mapped_column(Date)
    payer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("parties.id"))
    supplier: Mapped[str | None] = mapped_column(String(255))
    item: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(128))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    note: Mapped[str | None] = mapped_column(Text)
    source_sheet: Mapped[str | None] = mapped_column(String(128))
    source_row: Mapped[int | None]

    asset: Mapped[Asset | None] = relationship()
    payer: Mapped[Party | None] = relationship()


class StockTransaction(Base):
    __tablename__ = "stock_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    traded_on: Mapped[date | None] = mapped_column(Date)
    instrument_type: Mapped[str | None] = mapped_column(String(64))
    movement_type: Mapped[str | None] = mapped_column(String(64))
    cluster: Mapped[str | None] = mapped_column(String(64))
    instrument_name: Mapped[str | None] = mapped_column(String(255))
    isin: Mapped[str | None] = mapped_column(String(32))
    ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    market: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    unit_price_ccy: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    limit_ai: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    gross_amount_ccy: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    currency: Mapped[str | None] = mapped_column(String(8))
    fee_ccy: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    fee_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    amount_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    difference_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    difference_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    description: Mapped[str | None] = mapped_column(Text)
    source_row: Mapped[int | None]


class WatchlistStock(Base):
    __tablename__ = "watchlist_stocks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    watched_on: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    name: Mapped[str | None] = mapped_column(String(255))
    isin: Mapped[str | None] = mapped_column(String(32))
    ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    currency: Mapped[str | None] = mapped_column(String(8))
    difference_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    week_52_max: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    week_52_state_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    note: Mapped[str | None] = mapped_column(Text)
    owned_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    profit_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    source_row: Mapped[int | None]


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    __table_args__ = (UniqueConstraint("rate_date", "currency", name="uq_exchange_rate_date_currency"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rate_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(8))
    rate_to_czk: Mapped[Decimal] = mapped_column(Numeric(16, 6))


class TickerDescription(Base):
    __tablename__ = "ticker_descriptions"

    ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    isin: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)


class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"

    ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    currency: Mapped[str | None] = mapped_column(String(8))
    market_value_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    invested_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    profit_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    portfolio_share_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    first_buy_date: Mapped[date | None] = mapped_column(Date)
    source_row: Mapped[int | None]


class DailyStatistic(Base):
    __tablename__ = "daily_statistics"

    stat_date: Mapped[date] = mapped_column(Date, primary_key=True)
    bought_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    total_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    eur_in_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    value_eur: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    eur_rate: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    bought_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    total_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    usd_in_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    value_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    bought_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    total_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    value_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    invested_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    total_value_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    unrealized_profit_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    dividends: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    dividends_total: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    profit_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    daily_profit_czk: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    alerts: Mapped[str | None] = mapped_column(Text)
