"use client";

import {
  AlertTriangle,
  BarChart3,
  Building2,
  Calculator,
  CalendarDays,
  ChevronDown,
  ChevronRight,
  Coins,
  Download,
  FileSpreadsheet,
  Layers,
  ListChecks,
  LineChart,
  Lock,
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Save,
  Settings as SettingsIcon,
  ShieldCheck,
  TrendingUp,
  WalletCards,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8010";

type Summary = {
  loans_total: number;
  assets_total: number;
  asset_costs_total: number;
  portfolio_value_czk: number;
  portfolio_profit_czk: number;
  latest_stat: Record<string, unknown> | null;
  counts: Record<string, number>;
};

type Row = Record<string, string | number | null | boolean | Record<string, unknown> | unknown[]>;

type LatestRates = {
  rate_date: string | null;
  rates: Row[];
};

type StockOverview = {
  movements: Row[];
  currencies: Row[];
  top_profit: Row[];
  top_loss: Row[];
};

type Alerts = {
  threshold_pct: number;
  watchlist_limit_breaches: Row[];
  portfolio_drawdowns: Row[];
  daily_movers: Row[];
  daily_movers_as_of: string | null;
};

type TickerHistoryResult = {
  ticker: string;
  currency: string | null;
  rows: Row[];
  summary: Row | null;
};

type AssetAgenda = {
  asset: Row;
  cost_total: number;
  cost_count: number;
  costs: Row[];
  categories: Row[];
};

// A "Subjekt" in the UI (kept as "Portfolio"/portfolio_id internally/in the
// API - "Subjekt" is only the user-facing Czech label, chosen to avoid
// colliding with the pre-existing "portfolio" tab, which is the stock
// portfolio and unrelated to this grouping entity).
type PortfolioGrant = {
  id: string;
  name: string;
  allowed_agendas: string[];
};

type CurrentUser = {
  username: string;
  full_name: string | null;
  is_active: boolean;
  is_admin: boolean;
  allowed_agendas: string[];
  totp_enabled: boolean;
  alert_daily_change_pct: number | null;
  alert_drop_pct: number | null;
  portfolios: PortfolioGrant[];
};

type TwoFactorSetup = {
  secret: string;
  otpauth_uri: string;
  qr_code_png_base64: string;
};

const tabs = [
  { id: "assets", label: "Majetek", icon: Building2, endpoint: "/assets" },
  { id: "costs", label: "Náklady", icon: WalletCards, endpoint: "/assets/costs" },
  { id: "loans", label: "Půjčky", icon: Coins, endpoint: "/loans/movements" },
  { id: "transactions", label: "Transakce", icon: FileSpreadsheet, endpoint: "/stocks/transactions" },
  { id: "watchlist", label: "Sledované", icon: ListChecks, endpoint: "/stocks/watchlist" },
  { id: "stats", label: "Denní statistika", icon: ShieldCheck, endpoint: "/stocks/statistics" },
  { id: "portfolio", label: "Portfolio", icon: LineChart, endpoint: "/stocks/portfolio" },
  { id: "history", label: "Sledování akcie", icon: TrendingUp, endpoint: "/stocks/transactions" },
  { id: "alerts", label: "Upozornění", icon: AlertTriangle, endpoint: "/stocks/alerts" },
  { id: "charts", label: "Grafy", icon: BarChart3, endpoint: "/stocks/statistics" },
  { id: "rates", label: "Denní kurzy", icon: CalendarDays, endpoint: "/rates/daily?limit=500" },
  { id: "subjects", label: "Subjekty", icon: Layers, endpoint: "/portfolios" },
  { id: "users", label: "Uživatelé", icon: Lock, endpoint: "/users" },
  { id: "settings", label: "Nastavení", icon: SettingsIcon, endpoint: "/auth/me" },
];

// "settings" (personal notification preferences) is intentionally not listed
// here - it's always visible to every logged-in user regardless of agenda
// permissions, see visibleTabs/visibleNavGroups below.
const navGroups = [
  { label: "Majetek", items: ["assets", "costs"] },
  { label: "Půjčky", items: ["loans"] },
  { label: "Akcie", items: ["transactions", "watchlist", "stats", "portfolio", "history", "alerts", "charts"] },
  { label: "Nastavení", items: ["rates", "subjects", "users", "settings"] },
];

const tabsById = Object.fromEntries(tabs.map((tab) => [tab.id, tab]));

// Tabs that show the "Akciový souhrn" (movements/currencies mini-grid + price
// refresh/recalculate actions). "stats" is deliberately excluded - that tab
// now shows only its own movements table (Upozorneni/Grafy moved to their
// own tabs).
const STOCK_OVERVIEW_TABS = ["transactions", "watchlist", "portfolio"];

// Tabs that render their own custom panel instead of the generic data table.
const NON_TABLE_TABS = new Set(["assets", "history", "alerts", "charts", "settings", "subjects"]);

// "rates" (shared CNB exchange-rate history), "users" (user management) and
// "subjects" (Subjekt management) apply app-wide and stay governed by
// AppUser.allowed_agendas. Every other tab is scoped per-Subjekt instead -
// see isTabVisible below.
const GLOBAL_AGENDAS = new Set(["rates", "users", "subjects"]);
const PORTFOLIO_SCOPED_TABS = tabs.filter((tab) => !GLOBAL_AGENDAS.has(tab.id) && tab.id !== "settings");

const columns: Record<string, string[]> = {
  portfolio: ["ticker", "name", "quantity", "currency", "market_value_czk", "invested_czk", "profit_czk", "profit_pct"],
  assets: ["code", "owner", "asset_type", "name", "total_value", "own_funds", "borrowed_amount", "interest_rate"],
  costs: ["cost_date", "asset", "payer", "supplier", "category", "item", "amount"],
  loans: ["period_label", "lender", "borrower", "amount", "interest_rate", "planned_end_date", "description"],
  transactions: [
    "traded_on",
    "instrument_type",
    "movement_type",
    "cluster",
    "instrument_name",
    "isin",
    "ticker",
    "market",
    "quantity",
    "unit_price_ccy",
    "limit_ai",
    "gross_amount_ccy",
    "currency",
    "fee_ccy",
    "fee_czk",
    "amount_czk",
    "current_price",
    "difference_czk",
    "difference_pct",
    "description",
  ],
  watchlist: [
    "watched_on",
    "reason",
    "quantity",
    "name",
    "isin",
    "ticker",
    "limit_price",
    "current_price",
    "currency",
    "difference_pct",
    "week_52_max",
    "week_52_state_pct",
    "note",
    "owned_value",
    "profit_loss_pct",
    "new_price",
  ],
  rates: ["rate_date", "eur", "usd"],
  users: ["username", "full_name", "is_admin", "is_active", "totp_enabled", "allowed_agendas"],
  stats: [
    "period_label",
    "bought_eur",
    "total_eur",
    "eur_in_czk",
    "value_eur",
    "eur_rate",
    "bought_usd",
    "total_usd",
    "usd_in_czk",
    "value_usd",
    "usd_rate",
    "bought_czk",
    "total_czk",
    "value_czk",
    "invested_czk",
    "total_value_czk",
    "unrealized_profit_czk",
    "dividends",
    "dividends_total",
    "profit_pct",
    "daily_profit_czk",
    "alerts",
  ],
};

const costColumns = ["cost_date", "payer", "supplier", "category", "item", "amount", "note"];
const statSumColumns = ["bought_eur", "bought_usd", "bought_czk", "dividends", "daily_profit_czk"];
const statLatestColumns = [
  "total_eur",
  "eur_in_czk",
  "value_eur",
  "eur_rate",
  "total_usd",
  "usd_in_czk",
  "value_usd",
  "usd_rate",
  "total_czk",
  "value_czk",
  "invested_czk",
  "total_value_czk",
  "unrealized_profit_czk",
  "dividends_total",
  "profit_pct",
];

const labels: Record<string, string> = {
  ticker: "Ticker",
  name: "Název",
  quantity: "Počet",
  currency: "Měna",
  market_value_czk: "Hodnota CZK",
  invested_czk: "Investice CZK",
  profit_czk: "Zisk CZK",
  profit_pct: "Zisk %",
  code: "Kód",
  owner: "Vlastník",
  asset_type: "Typ",
  total_value: "Celkem",
  own_funds: "Vlastní zdroje",
  borrowed_amount: "Půjčeno",
  interest_rate: "Úrok",
  cost_date: "Datum",
  asset: "Majetek",
  payer: "Plátce",
  supplier: "Dodavatel",
  category: "Kategorie",
  item: "Položka",
  amount: "Částka",
  movement_date: "Datum",
  period_label: "Období",
  lender: "Věřitel",
  borrower: "Dlužník",
  planned_end_date: "Plán konec",
  description: "Popis",
  traded_on: "Datum",
  instrument_type: "Typ",
  movement_type: "Pohyb",
  cluster: "Cluster",
  instrument_name: "Akcie",
  isin: "ISIN",
  market: "Trh",
  unit_price_ccy: "Cena MJ",
  limit_ai: "Limit AI",
  gross_amount_ccy: "Cena CM",
  fee_ccy: "Poplatek CM",
  fee_czk: "Poplatek CZK",
  amount_czk: "Cena CZK",
  current_price: "Aktuální cena",
  difference_czk: "Rozdíl CZK",
  difference_pct: "Rozdíl %",
  watched_on: "Datum",
  reason: "Důvod",
  limit_price: "Limitní cena",
  week_52_max: "52T max.",
  week_52_state_pct: "Stav 52T max.",
  owned_value: "Hodnota vlastněných",
  profit_loss_pct: "Zisk/Ztráta",
  new_price: "Nová cena",
  rate_date: "Datum",
  rate_to_czk: "Kurz CZK",
  eur: "EUR",
  usd: "USD",
  count: "Počet",
  amount_ccy: "Částka v měně",
  stat_date: "Datum",
  bought_eur: "Nák. EUR",
  total_eur: "Σ EUR",
  eur_in_czk: "EUR v CZK",
  value_eur: "Hod. EUR",
  eur_rate: "Kurz EUR",
  bought_usd: "Nák. USD",
  total_usd: "Σ USD",
  usd_in_czk: "USD v CZK",
  value_usd: "Hod. USD",
  usd_rate: "Kurz USD",
  bought_czk: "Nák. CZK",
  total_czk: "Σ CZK",
  value_czk: "Hod. CZK",
  total_value_czk: "Hodnota CZK",
  unrealized_profit_czk: "Nereal. zisk",
  dividends: "Dividendy",
  dividends_total: "Dividendy",
  daily_profit_czk: "Denní zisk",
  alerts: "Varování",
  note: "Poznámka",
  username: "Uživatelské jméno",
  full_name: "Jméno",
  is_admin: "Administrátor",
  is_active: "Aktivní",
  allowed_agendas: "Povolené agendy",
  totp_enabled: "2FA",
  trade_date: "Datum",
  rate: "Kurz",
  price_ccy: "Cena (CM)",
  price_czk: "Cena CZK",
  change_pct: "Zm. %",
  buy_quantity: "Nákup ks",
  buy_ccy: "Nákup CM",
  buy_czk: "Nákup CZK",
  cumulative_quantity: "Celk. ks",
  value_ccy: "Hodnota CM",
  change_value_ccy: "Zm. CM",
  change_value_czk: "Zm. CZK",
  profit_ccy: "Zisk/Ztr. CM",
  invested_ccy: "Investice CM",
};

const historyColumns = [
  "trade_date",
  "price_ccy",
  "rate",
  "price_czk",
  "change_pct",
  "buy_quantity",
  "buy_ccy",
  "buy_czk",
  "cumulative_quantity",
  "value_ccy",
  "value_czk",
  "change_value_ccy",
  "change_value_czk",
  "profit_ccy",
  "profit_czk",
];

function RecalcFromField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <div className="recalc-from">
      <label>
        Napočítat od
        <span className="recalc-from-input-row">
          <input type="date" autoComplete="off" value={value} onChange={(event) => onChange(event.target.value)} />
          {value && (
            <button
              type="button"
              className="recalc-from-clear"
              onClick={() => onChange("")}
              title="Vymazat datum - příští přepočet pak projde celou historii"
            >
              <X size={14} />
            </button>
          )}
        </span>
      </label>
      <span className="field-hint">
        {value ? `Přepočítají se jen dny od ${value} dál, starší řádky zůstanou beze změny.` : "Prázdné = přepočítá se celá historie."}
      </span>
    </div>
  );
}

function interestPlanEntries(asset: Row): [string, number][] {
  const plan = asset.computed_interest_plan;
  if (!plan || typeof plan !== "object" || Array.isArray(plan)) return [];
  return Object.entries(plan as Record<string, unknown>)
    .filter((entry): entry is [string, number] => typeof entry[1] === "number")
    .sort((a, b) => a[0].localeCompare(b[0]));
}

type ChartPoint = { date: string; [key: string]: number | string };
type ChartSeriesDef = { key: string; label: string; color: string };

// Lightweight custom SVG charts - no charting library dependency, so a Docker
// rebuild never needs to fetch a new npm package. Port of AkcieStatistika.bas's
// "Graf" sheet (Hodnota vs Investice / Nerealizovany zisk / Dividendy), fed
// from the same daily_statistics rows already shown in the table below.
function PortfolioChart({
  title,
  data,
  mode,
  series,
  formatValue: formatY,
  large = false,
}: {
  title: string;
  data: ChartPoint[];
  mode: "lines" | "area" | "diverging-area";
  series: ChartSeriesDef[];
  formatValue: (value: number) => string;
  large?: boolean;
}) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  if (data.length < 2) {
    return (
      <div className="portfolio-chart portfolio-chart-empty">
        <h3>{title}</h3>
        <p className="field-hint">Zatím málo dat pro graf.</p>
      </div>
    );
  }

  const width = 640;
  const height = 220;
  const marginLeft = 8;
  const marginRight = 8;
  const marginTop = 12;
  const marginBottom = 22;
  const plotWidth = width - marginLeft - marginRight;
  const plotHeight = height - marginTop - marginBottom;

  const allValues: number[] = [0];
  for (const point of data) {
    for (const s of series) allValues.push(numberValue(point[s.key]));
  }
  let min = Math.min(...allValues);
  let max = Math.max(...allValues);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const pad = (max - min) * 0.08;
  min -= pad;
  max += pad;

  const xAt = (index: number) => marginLeft + (data.length === 1 ? plotWidth / 2 : (index / (data.length - 1)) * plotWidth);
  const yAt = (value: number) => marginTop + plotHeight - ((value - min) / (max - min)) * plotHeight;
  const zeroY = yAt(0);

  const yTickCount = 4;
  const yTicks = Array.from({ length: yTickCount }, (_, i) => min + ((max - min) * i) / (yTickCount - 1));

  function linePath(key: string) {
    return data.map((point, index) => `${index === 0 ? "M" : "L"} ${xAt(index).toFixed(1)} ${yAt(numberValue(point[key])).toFixed(1)}`).join(" ");
  }

  function areaPath(key: string, clamp: "positive" | "negative" | null) {
    const values = data.map((point) => {
      const raw = numberValue(point[key]);
      if (clamp === "positive") return Math.max(raw, 0);
      if (clamp === "negative") return Math.min(raw, 0);
      return raw;
    });
    const top = values.map((value, index) => `${index === 0 ? "M" : "L"} ${xAt(index).toFixed(1)} ${yAt(value).toFixed(1)}`).join(" ");
    return `${top} L ${xAt(data.length - 1).toFixed(1)} ${zeroY.toFixed(1)} L ${xAt(0).toFixed(1)} ${zeroY.toFixed(1)} Z`;
  }

  function handleMove(event: ReactMouseEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width === 0) return;
    const relativeX = ((event.clientX - rect.left) / rect.width) * width;
    const ratio = (relativeX - marginLeft) / plotWidth;
    const index = Math.round(ratio * (data.length - 1));
    setHoverIndex(Math.min(Math.max(index, 0), data.length - 1));
  }

  const hoverPoint = hoverIndex !== null ? data[hoverIndex] : null;

  return (
    <div className={`portfolio-chart${large ? " portfolio-chart-large" : ""}`}>
      <div className="portfolio-chart-header">
        <h3>{title}</h3>
        {mode === "lines" && (
          <div className="portfolio-chart-legend">
            {series.map((s) => (
              <span key={s.key} className="portfolio-chart-legend-item">
                <span className="portfolio-chart-swatch" style={{ background: s.color }} />
                {s.label}
              </span>
            ))}
          </div>
        )}
        {mode === "diverging-area" && (
          <div className="portfolio-chart-legend">
            <span className="portfolio-chart-legend-item">
              <span className="portfolio-chart-swatch positive" />
              Zisk
            </span>
            <span className="portfolio-chart-legend-item">
              <span className="portfolio-chart-swatch negative" />
              Ztráta
            </span>
          </div>
        )}
      </div>
      <div className="portfolio-chart-plot">
        <div className="portfolio-chart-yaxis">
          {yTicks
            .slice()
            .reverse()
            .map((tick, index) => (
              <span key={index} style={{ top: `${((yAt(tick) / height) * 100).toFixed(2)}%` }}>
                {formatY(tick)}
              </span>
            ))}
        </div>
        <svg
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          className="portfolio-chart-svg"
          onMouseMove={handleMove}
          onMouseLeave={() => setHoverIndex(null)}
          role="img"
          aria-label={title}
        >
          {yTicks.map((tick, index) => (
            <line
              key={index}
              x1={marginLeft}
              x2={width - marginRight}
              y1={yAt(tick)}
              y2={yAt(tick)}
              className="portfolio-chart-gridline"
            />
          ))}
          {mode !== "lines" && (
            <line x1={marginLeft} x2={width - marginRight} y1={zeroY} y2={zeroY} className="portfolio-chart-baseline" />
          )}
          {mode === "lines" &&
            series.map((s) => <path key={s.key} d={linePath(s.key)} fill="none" stroke={s.color} strokeWidth={2} />)}
          {mode === "area" && (
            <path d={areaPath(series[0].key, null)} fill={series[0].color} fillOpacity={0.22} stroke={series[0].color} strokeWidth={2} />
          )}
          {mode === "diverging-area" && (
            <>
              <path d={areaPath(series[0].key, "positive")} className="portfolio-chart-fill positive" stroke="none" />
              <path d={areaPath(series[0].key, "negative")} className="portfolio-chart-fill negative" stroke="none" />
              <path d={linePath(series[0].key)} fill="none" stroke="var(--accent)" strokeWidth={1.5} />
            </>
          )}
          {hoverPoint && (
            <line
              x1={xAt(hoverIndex as number)}
              x2={xAt(hoverIndex as number)}
              y1={marginTop}
              y2={marginTop + plotHeight}
              className="portfolio-chart-crosshair"
            />
          )}
        </svg>
      </div>
      <div className="portfolio-chart-axis">
        <span>{formatDateWithWeekday(String(data[0].date))}</span>
        <span>{formatDateWithWeekday(String(data[data.length - 1].date))}</span>
      </div>
      {hoverPoint && (
        <div className="portfolio-chart-tooltip">
          <strong>{formatDateWithWeekday(String(hoverPoint.date))}</strong>
          {series.map((s) => (
            <span key={s.key}>
              {s.label}: {formatY(numberValue(hoverPoint[s.key]))}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// Fields that are genuinely a fraction (0.05 = 5 %). Everything else called
// "*_rate" (eur_rate, usd_rate, rate_to_czk, the ticker-history "rate" column)
// is an actual exchange rate/kurz (e.g. 24.685 CZK per EUR), not a percentage -
// a plain substring match on "rate" used to misclassify those as percent.
const PERCENT_KEYS = new Set(["profit_pct", "difference_pct", "profit_loss_pct", "week_52_state_pct", "change_pct", "interest_rate"]);

function formatValue(key: string, value: Row[string]) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "boolean") return value ? "ano" : "ne";
  if (Array.isArray(value)) return value.map((item) => tabsById[String(item)]?.label || String(item)).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "number") {
    if (PERCENT_KEYS.has(key)) {
      return new Intl.NumberFormat("cs-CZ", { style: "percent", maximumFractionDigits: 2 }).format(value);
    }
    return new Intl.NumberFormat("cs-CZ", { maximumFractionDigits: 2 }).format(value);
  }
  return String(value);
}

const STAT_RATE_COLUMNS = new Set(["eur_rate", "usd_rate"]);

// Same fixed decimal counts as the Excel sheet's number formats ("#,##0" for
// money, "#,##0.0000" for Kurz EUR/USD) - a FIXED (not maximum) number of
// decimal places everywhere, so the digits line up column by column instead
// of drifting depending on whether a given day happens to be a whole number.
function formatStatValue(key: string, value: Row[string]) {
  if (typeof value !== "number") return formatValue(key, value);
  if (PERCENT_KEYS.has(key)) {
    return new Intl.NumberFormat("cs-CZ", { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
  }
  if (STAT_RATE_COLUMNS.has(key)) {
    return new Intl.NumberFormat("cs-CZ", { minimumFractionDigits: 4, maximumFractionDigits: 4 }).format(value);
  }
  return new Intl.NumberFormat("cs-CZ", { minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(value);
}

function isNumericCell(value: Row[string]) {
  return typeof value === "number";
}

function money(value: number) {
  return new Intl.NumberFormat("cs-CZ", { style: "currency", currency: "CZK", maximumFractionDigits: 0 }).format(value || 0);
}

function rateForCurrency(latestRates: LatestRates | null, currency: string) {
  return latestRates?.rates.find((rate) => String(rate.currency) === currency)?.rate_to_czk ?? null;
}

function numberValue(value: Row[string]) {
  return typeof value === "number" ? value : 0;
}

function monthLabel(dateText: string) {
  const date = new Date(`${dateText}T00:00:00`);
  return new Intl.DateTimeFormat("cs-CZ", { month: "long", year: "numeric" }).format(date);
}

const CZ_WEEKDAYS = ["Ne", "Po", "Út", "St", "Čt", "Pá", "So"]; // indexed by Date#getDay()

// Same as the "ddd" day-of-week + "DD.MM.YYYY" date columns in AkcieStatistika.bas.
function formatDateWithWeekday(dateText: string) {
  if (!dateText) return "";
  const parsed = new Date(`${dateText}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return dateText;
  const day = CZ_WEEKDAYS[parsed.getDay()];
  const dd = String(parsed.getDate()).padStart(2, "0");
  const mm = String(parsed.getMonth() + 1).padStart(2, "0");
  return `${day} ${dd}.${mm}.${parsed.getFullYear()}`;
}

// Same as the VBA "+#,##0;-#,##0;0" number format + green/red font on
// Nereal. zisk / Zisk denní.
function formatSignedProfit(value: number) {
  const rounded = Math.round(value);
  const formatted = new Intl.NumberFormat("cs-CZ", { maximumFractionDigits: 0 }).format(Math.abs(rounded));
  if (rounded > 0) return `+${formatted}`;
  if (rounded < 0) return `-${formatted}`;
  return "0";
}

const signedProfitColumns = new Set(["unrealized_profit_czk", "daily_profit_czk"]);

// Sledování akcie (ticker price-history) table: fixed 2 decimal places
// everywhere, blank cells instead of "0" for missing/zero data (matches the
// source Excel's own convention - "Prázdné buňky = data nejsou k dispozici"),
// and the same signed +green / -red styling as the profit columns above, but
// kept to 2 decimal places instead of being rounded to whole numbers.
const HISTORY_SIGNED_COLUMNS = new Set(["change_pct", "change_value_ccy", "change_value_czk", "profit_ccy", "profit_czk"]);

function formatHistoryValue(key: string, value: Row[string]) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value !== "number") return String(value);
  if (value === 0) return "";
  if (key === "change_pct") {
    return new Intl.NumberFormat("cs-CZ", { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
  }
  return new Intl.NumberFormat("cs-CZ", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

function formatSignedProfitPrecise(value: number, isPercent = false) {
  if (!value) return "";
  const formatted = isPercent
    ? new Intl.NumberFormat("cs-CZ", { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(value))
    : new Intl.NumberFormat("cs-CZ", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(value));
  return value > 0 ? `+${formatted}` : `-${formatted}`;
}

function buildStatisticRows(rows: Row[], expandedMonths: Set<string>) {
  const groups = new Map<string, Row[]>();
  for (const row of rows) {
    const dateText = String(row.stat_date || "");
    if (!dateText) continue;
    const monthKey = dateText.slice(0, 7);
    const group = groups.get(monthKey) || [];
    group.push(row);
    groups.set(monthKey, group);
  }

  const result: Row[] = [];
  for (const [monthKey, groupRows] of [...groups.entries()].sort((a, b) => b[0].localeCompare(a[0]))) {
    const sortedRows = [...groupRows].sort((a, b) => String(b.stat_date).localeCompare(String(a.stat_date)));
    const latest = sortedRows[0];
    const expanded = expandedMonths.has(monthKey);
    const summary: Row = {
      row_kind: "summary",
      month_key: monthKey,
      period_label: monthLabel(`${monthKey}-01`),
      alerts: sortedRows.map((row) => row.alerts).filter(Boolean).join(" | "),
    };
    for (const col of statSumColumns) {
      summary[col] = sortedRows.reduce((total, row) => total + numberValue(row[col]), 0);
    }
    for (const col of statLatestColumns) {
      summary[col] = latest[col] ?? null;
    }
    result.push(summary);

    if (expanded) {
      for (const row of sortedRows) {
        result.push({ ...row, row_kind: "detail", period_label: formatDateWithWeekday(String(row.stat_date || "")) });
      }
    }
  }
  return result;
}

function buildCostRows(rows: Row[], assetFilter: string, showDetail: boolean) {
  const filteredRows = assetFilter ? rows.filter((row) => String(row.asset || "") === assetFilter) : rows;
  const groups = new Map<string, Row[]>();
  for (const row of filteredRows) {
    const dateText = String(row.cost_date || "");
    const monthKey = dateText ? dateText.slice(0, 7) : "bez-data";
    const group = groups.get(monthKey) || [];
    group.push(row);
    groups.set(monthKey, group);
  }

  const result: Row[] = [];
  for (const [monthKey, groupRows] of [...groups.entries()].sort((a, b) => b[0].localeCompare(a[0]))) {
    const sortedRows = [...groupRows].sort((a, b) => String(b.cost_date || "").localeCompare(String(a.cost_date || "")));
    const summary: Row = {
      row_kind: "summary",
      cost_date: monthKey === "bez-data" ? "Bez data" : monthLabel(`${monthKey}-01`),
      asset: assetFilter || "Všechny investice",
      item: "Měsíční mezisoučet",
      amount: sortedRows.reduce((total, row) => total + numberValue(row.amount), 0),
    };
    result.push(summary);
    if (showDetail) {
      for (const row of sortedRows) {
        result.push({ ...row, row_kind: "detail" });
      }
    }
  }
  return result;
}

// Two-level collapsible subtotals (year, then month within it) for the loans
// table - the source Excel had these subtotal rows baked directly into the
// data (a "Leden 2023"/"2023" row physically sitting between real
// movements); the app now computes them itself instead of importing them as
// if they were real loan movements.
function buildLoanRows(rows: Row[], expandedYears: Set<string>, expandedMonths: Set<string>) {
  const byYear = new Map<string, Row[]>();
  for (const row of rows) {
    const dateText = String(row.movement_date || "");
    if (!dateText) continue;
    const year = dateText.slice(0, 4);
    const group = byYear.get(year) || [];
    group.push(row);
    byYear.set(year, group);
  }

  const result: Row[] = [];
  for (const [year, yearRows] of [...byYear.entries()].sort((a, b) => b[0].localeCompare(a[0]))) {
    result.push({
      row_kind: "year-summary",
      year_key: year,
      period_label: `Rok ${year}`,
      amount: yearRows.reduce((total, row) => total + numberValue(row.amount), 0),
    });
    if (!expandedYears.has(year)) continue;

    const byMonth = new Map<string, Row[]>();
    for (const row of yearRows) {
      const monthKey = String(row.movement_date).slice(0, 7);
      const group = byMonth.get(monthKey) || [];
      group.push(row);
      byMonth.set(monthKey, group);
    }
    for (const [monthKey, monthRows] of [...byMonth.entries()].sort((a, b) => b[0].localeCompare(a[0]))) {
      result.push({
        row_kind: "month-summary",
        year_key: year,
        month_key: monthKey,
        period_label: monthLabel(`${monthKey}-01`),
        amount: monthRows.reduce((total, row) => total + numberValue(row.amount), 0),
      });
      if (!expandedMonths.has(monthKey)) continue;

      const sortedRows = [...monthRows].sort((a, b) => String(b.movement_date || "").localeCompare(String(a.movement_date || "")));
      for (const row of sortedRows) {
        result.push({ ...row, row_kind: "detail", period_label: formatDateWithWeekday(String(row.movement_date || "")) });
      }
    }
  }
  return result;
}

export default function Page() {
  const [token, setToken] = useState<string | null>(null);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [pendingToken, setPendingToken] = useState<string | null>(null);
  const [twoFactorCode, setTwoFactorCode] = useState("");
  const [twoFactorBusy, setTwoFactorBusy] = useState(false);
  const [twoFactorSetup, setTwoFactorSetup] = useState<TwoFactorSetup | null>(null);
  const [twoFactorSetupCode, setTwoFactorSetupCode] = useState("");
  const [twoFactorSetupBusy, setTwoFactorSetupBusy] = useState(false);
  const [twoFactorDisableOpen, setTwoFactorDisableOpen] = useState(false);
  const [twoFactorDisablePassword, setTwoFactorDisablePassword] = useState("");
  const [twoFactorDisableCode, setTwoFactorDisableCode] = useState("");
  const [twoFactorDisableBusy, setTwoFactorDisableBusy] = useState(false);
  const [twoFactorStatus, setTwoFactorStatus] = useState<string | null>(null);
  const [adminResetUsername, setAdminResetUsername] = useState("");
  const [adminResetBusy, setAdminResetBusy] = useState(false);
  const [adminResetStatus, setAdminResetStatus] = useState<string | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [activeTab, setActiveTab] = useState(tabs[0].id);
  const [rows, setRows] = useState<Row[]>([]);
  const [latestRates, setLatestRates] = useState<LatestRates | null>(null);
  const [stockOverview, setStockOverview] = useState<StockOverview | null>(null);
  const [alerts, setAlerts] = useState<Alerts | null>(null);
  const [assetAgendas, setAssetAgendas] = useState<AssetAgenda[]>([]);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [expandedStatMonths, setExpandedStatMonths] = useState<Set<string>>(() => new Set());
  const [expandedLoanYears, setExpandedLoanYears] = useState<Set<string>>(() => new Set());
  const [expandedLoanMonths, setExpandedLoanMonths] = useState<Set<string>>(() => new Set());
  const [loanCleanupStatus, setLoanCleanupStatus] = useState<string | null>(null);
  const [loanCleanupBusy, setLoanCleanupBusy] = useState(false);
  const [showCostDetail, setShowCostDetail] = useState(true);
  const [costAssetFilter, setCostAssetFilter] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [rateDate, setRateDate] = useState(new Date().toISOString().slice(0, 10));
  const [rateCurrency, setRateCurrency] = useState("EUR");
  const [rateValue, setRateValue] = useState("");
  const [patriaText, setPatriaText] = useState("");
  const [stockActionStatus, setStockActionStatus] = useState<string | null>(null);
  const [stockBusy, setStockBusy] = useState(false);
  const [historyTicker, setHistoryTicker] = useState("");
  const [historyFrom, setHistoryFrom] = useState(
    new Date(Date.now() - 90 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10),
  );
  const [historyTo, setHistoryTo] = useState(new Date().toISOString().slice(0, 10));
  const [historyResult, setHistoryResult] = useState<TickerHistoryResult | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [recalcFromDate, setRecalcFromDate] = useState("");
  const [workingMessage, setWorkingMessage] = useState<string | null>(null);
  const [newUser, setNewUser] = useState({
    username: "",
    password: "",
    full_name: "",
    is_admin: false,
    allowed_agendas: [] as string[],
  });
  const [notifDailyChangePct, setNotifDailyChangePct] = useState("");
  const [notifDropPct, setNotifDropPct] = useState("");
  const [notifBusy, setNotifBusy] = useState(false);
  const [notifStatus, setNotifStatus] = useState<string | null>(null);
  const [activePortfolioId, setActivePortfolioId] = useState<string | null>(null);
  const latestLoadRequestRef = useRef(0);
  const [portfolios, setPortfolios] = useState<{ id: string; name: string }[]>([]);
  const [newPortfolioName, setNewPortfolioName] = useState("");
  const [portfolioBusy, setPortfolioBusy] = useState(false);
  const [portfolioStatus, setPortfolioStatus] = useState<string | null>(null);
  const [renamingPortfolioId, setRenamingPortfolioId] = useState<string | null>(null);
  const [renamePortfolioName, setRenamePortfolioName] = useState("");
  const [editingAccessUsername, setEditingAccessUsername] = useState<string | null>(null);
  const [accessDraft, setAccessDraft] = useState<Record<string, string[]>>({});
  const [accessBusy, setAccessBusy] = useState(false);
  const [accessStatus, setAccessStatus] = useState<string | null>(null);
  const [editingUserUsername, setEditingUserUsername] = useState<string | null>(null);
  const [editUserDraft, setEditUserDraft] = useState({
    full_name: "",
    is_admin: false,
    is_active: true,
    allowed_agendas: [] as string[],
    password: "",
  });
  const [editUserBusy, setEditUserBusy] = useState(false);
  const [editUserStatus, setEditUserStatus] = useState<string | null>(null);
  const allowedAgendaSet = useMemo(
    () => new Set(currentUser?.is_admin ? tabs.map((tab) => tab.id) : currentUser?.allowed_agendas || tabs.map((tab) => tab.id)),
    [currentUser],
  );
  // Falls back to the first accessible Subjekt even before the
  // activePortfolioId-reconciliation effect below has had a chance to run
  // (e.g. right after login, in the same render currentUser first becomes
  // available) - otherwise isTabVisible would briefly see no active
  // Subjekt and redirect away from the default tab to whatever global tab
  // happens to be visible.
  const activePortfolio = useMemo(
    () => currentUser?.portfolios.find((portfolio) => portfolio.id === activePortfolioId) ?? currentUser?.portfolios[0] ?? null,
    [currentUser, activePortfolioId],
  );
  // "settings" is personal preferences, not a data agenda - every logged-in
  // user can reach it regardless of what allowed_agendas grants them.
  // "rates"/"users" are the two agendas that stay app-wide (AppUser.allowed_
  // agendas); every other tab is scoped to the currently active Subjekt.
  // While currentUser hasn't loaded yet (token set, /auth/me still in
  // flight), allowedAgendaSet already optimistically defaults to "every tab"
  // (see above) rather than hiding everything - portfolio-scoped tabs must
  // match that during the same loading window, or the tab-visibility
  // reconciliation effect below redirects away from the default tab before
  // the real Subjekt grants are even known.
  const isTabVisible = (tabId: string) =>
    tabId === "settings"
      ? true
      : GLOBAL_AGENDAS.has(tabId)
        ? allowedAgendaSet.has(tabId)
        : currentUser
          ? Boolean(activePortfolio?.allowed_agendas.includes(tabId))
          : true;
  const visibleTabs = useMemo(() => tabs.filter((tab) => isTabVisible(tab.id)), [allowedAgendaSet, activePortfolio]);
  const visibleNavGroups = useMemo(
    () =>
      navGroups
        .map((group) => ({ ...group, items: group.items.filter((item) => isTabVisible(item)) }))
        .filter((group) => group.items.length > 0),
    [allowedAgendaSet, activePortfolio],
  );
  const active = useMemo(() => tabs.find((tab) => tab.id === activeTab) || visibleTabs[0] || tabs[0], [activeTab, visibleTabs]);
  const costAssets = useMemo(
    () => [...new Set(rows.map((row) => String(row.asset || "")).filter(Boolean))].sort((a, b) => a.localeCompare(b)),
    [rows],
  );
  const tableRows =
    activeTab === "stats"
      ? buildStatisticRows(rows, expandedStatMonths)
      : activeTab === "costs"
        ? buildCostRows(rows, costAssetFilter, showCostDetail)
        : activeTab === "loans"
          ? buildLoanRows(rows, expandedLoanYears, expandedLoanMonths)
          : rows;
  const chartData = useMemo(() => {
    if (activeTab !== "charts") return [];
    return [...rows]
      .filter((row) => row.stat_date)
      .sort((a, b) => String(a.stat_date).localeCompare(String(b.stat_date)))
      .map((row) => ({
        date: String(row.stat_date),
        invested_czk: numberValue(row.invested_czk),
        total_value_czk: numberValue(row.total_value_czk),
        unrealized_profit_czk: numberValue(row.unrealized_profit_czk),
        dividends_total: numberValue(row.dividends_total),
      }));
  }, [rows, activeTab]);

  function toggleStatMonth(monthKey: string) {
    setExpandedStatMonths((current) => {
      const next = new Set(current);
      if (next.has(monthKey)) {
        next.delete(monthKey);
      } else {
        next.add(monthKey);
      }
      return next;
    });
  }

  function toggleLoanYear(year: string) {
    setExpandedLoanYears((current) => {
      const next = new Set(current);
      if (next.has(year)) {
        next.delete(year);
      } else {
        next.add(year);
      }
      return next;
    });
  }

  function toggleLoanMonth(monthKey: string) {
    setExpandedLoanMonths((current) => {
      const next = new Set(current);
      if (next.has(monthKey)) {
        next.delete(monthKey);
      } else {
        next.add(monthKey);
      }
      return next;
    });
  }

  useEffect(() => {
    const saved = localStorage.getItem("finance-token");
    if (saved) setToken(saved);
    const savedPortfolio = localStorage.getItem("finance-portfolio-id");
    if (savedPortfolio) setActivePortfolioId(savedPortfolio);
  }, []);

  useEffect(() => {
    async function loadProfile() {
      if (!token) return;
      try {
        const profile = (await api("/auth/me")) as CurrentUser;
        setCurrentUser(profile);
      } catch {
        localStorage.removeItem("finance-token");
        setToken(null);
      }
    }
    loadProfile();
  }, [token]);

  // Keeps the active Subjekt valid whenever the user's grants change (login,
  // an admin editing access, …): falls back to the first accessible Subjekt
  // if none is selected yet or the saved one is no longer valid.
  useEffect(() => {
    if (!currentUser) return;
    if (activePortfolioId && currentUser.portfolios.some((portfolio) => portfolio.id === activePortfolioId)) return;
    setActivePortfolioId(currentUser.portfolios[0]?.id ?? null);
  }, [currentUser, activePortfolioId]);

  useEffect(() => {
    if (activePortfolioId) localStorage.setItem("finance-portfolio-id", activePortfolioId);
    else localStorage.removeItem("finance-portfolio-id");
  }, [activePortfolioId]);

  useEffect(() => {
    setNotifDailyChangePct(currentUser?.alert_daily_change_pct != null ? String(currentUser.alert_daily_change_pct) : "");
    setNotifDropPct(currentUser?.alert_drop_pct != null ? String(currentUser.alert_drop_pct) : "");
  }, [currentUser]);

  useEffect(() => {
    if (visibleTabs.length > 0 && !isTabVisible(activeTab)) {
      setActiveTab(visibleTabs[0].id);
    }
  }, [allowedAgendaSet, activeTab, visibleTabs]);

  async function api(path: string, options: RequestInit = {}) {
    const response = await fetch(`${API_URL}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers || {}),
      },
    });
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  // Appends the active Subjekt to a portfolio-scoped endpoint's query string.
  // Harmless (and skipped) for endpoints that ignore/don't need it - the API
  // silently ignores unknown query params, so this is safe to apply broadly.
  function withPortfolio(path: string): string {
    if (!activePortfolioId) return path;
    return `${path}${path.includes("?") ? "&" : "?"}portfolio_id=${activePortfolioId}`;
  }

  async function loadAll() {
    if (!token) return;
    // "rates"/"users"/"settings" don't need a Subjekt at all - every other
    // tab does, and there's nothing to load yet if the user has none.
    const tabNeedsPortfolio = !GLOBAL_AGENDAS.has(activeTab) && activeTab !== "settings";
    if (tabNeedsPortfolio && !activePortfolioId) return;
    // Guards against an older, slower-to-resolve loadAll() (e.g. from the
    // previously active tab) overwriting rows/summary/etc. with stale data
    // after the user has already switched tabs/Subjekty - fetches have no
    // cancellation, so without this the LAST FETCH TO FINISH wins, not the
    // last one STARTED.
    const requestId = ++latestLoadRequestRef.current;
    setError(null);
    try {
      const needsStockOverview = STOCK_OVERVIEW_TABS.includes(activeTab);
      const needsAlerts = activeTab === "alerts";
      // "alerts", "settings" and "subjects" render their own panel from
      // dedicated state (alerts/currentUser/portfolios) rather than the
      // generic rows table, and "alerts"/"settings"'s tab endpoint
      // ("/stocks/alerts", "/auth/me") returns a single object, not an array
      // - stuffing that into `rows` used to crash every unconditional
      // `rows.map` elsewhere (e.g. costAssets) as soon as you opened the tab.
      const needsRows = activeTab !== "alerts" && activeTab !== "settings" && activeTab !== "subjects";
      // Both "users" (per-user Subjekt-access editor) and "subjects" (create/
      // rename Subjekty themselves) need the full Subjekt list.
      const needsPortfolioList = (activeTab === "users" || activeTab === "subjects") && Boolean(currentUser?.is_admin);
      const requests: Promise<unknown>[] = [activePortfolioId ? api(withPortfolio("/summary")) : Promise.resolve(null)];
      if (needsRows) requests.push(api(withPortfolio(active.endpoint)));
      if (activeTab === "rates") requests.push(api("/rates/latest"));
      if (needsStockOverview) requests.push(api(withPortfolio("/stocks/overview")));
      if (needsAlerts) requests.push(api(withPortfolio("/stocks/alerts")));
      if (activeTab === "assets") requests.push(api(withPortfolio("/assets/agendas")));
      if (needsPortfolioList) requests.push(api("/portfolios"));
      const [summaryData, ...rest] = await Promise.all(requests);
      if (latestLoadRequestRef.current !== requestId) return;
      setSummary(summaryData as Summary | null);
      let restIndex = 0;
      setRows(needsRows ? (rest[restIndex++] as Row[]) : []);
      if (activeTab === "rates") setLatestRates(rest[restIndex++] as LatestRates);
      if (needsStockOverview) setStockOverview(rest[restIndex++] as StockOverview);
      if (needsAlerts) setAlerts(rest[restIndex++] as Alerts);
      if (activeTab === "assets") setAssetAgendas(rest[restIndex++] as AssetAgenda[]);
      if (needsPortfolioList) setPortfolios(rest[restIndex++] as { id: string; name: string }[]);
    } catch (err) {
      if (latestLoadRequestRef.current !== requestId) return;
      setError(err instanceof Error ? err.message : "Nepodařilo se načíst data");
    }
  }

  useEffect(() => {
    loadAll();
  }, [token, activeTab, activePortfolioId]);

  function logout() {
    localStorage.removeItem("finance-token");
    setToken(null);
    setCurrentUser(null);
    setPassword("");
    setPendingToken(null);
    setTwoFactorCode("");
    setError(null);
    setMobileMenuOpen(false);
  }

  async function login(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const response = await fetch(`${API_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) throw new Error("Neplatné přihlašovací údaje");
      const data = await response.json();
      if (data.requires_2fa) {
        setPendingToken(data.pending_token);
        setTwoFactorCode("");
        return;
      }
      localStorage.setItem("finance-token", data.token);
      setToken(data.token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Přihlášení selhalo");
    }
  }

  async function submitTwoFactorLogin(event: React.FormEvent) {
    event.preventDefault();
    if (!pendingToken) return;
    setError(null);
    setTwoFactorBusy(true);
    try {
      const response = await fetch(`${API_URL}/auth/2fa/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pending_token: pendingToken, code: twoFactorCode.trim() }),
      });
      if (!response.ok) throw new Error("Neplatný ověřovací kód");
      const data = await response.json();
      localStorage.setItem("finance-token", data.token);
      setToken(data.token);
      setPendingToken(null);
      setTwoFactorCode("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ověření se nezdařilo");
    } finally {
      setTwoFactorBusy(false);
    }
  }

  function cancelTwoFactorLogin() {
    setPendingToken(null);
    setTwoFactorCode("");
    setError(null);
  }

  async function startTwoFactorSetup() {
    setError(null);
    setTwoFactorStatus(null);
    setTwoFactorSetupBusy(true);
    try {
      const result = await api("/auth/2fa/setup", { method: "POST" });
      setTwoFactorSetup(result as TwoFactorSetup);
      setTwoFactorSetupCode("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Nastavení 2FA se nepodařilo spustit");
    } finally {
      setTwoFactorSetupBusy(false);
    }
  }

  async function confirmTwoFactorSetup(event: React.FormEvent) {
    event.preventDefault();
    if (!twoFactorSetup) return;
    setError(null);
    setTwoFactorSetupBusy(true);
    try {
      await api("/auth/2fa/confirm", {
        method: "POST",
        body: JSON.stringify({ secret: twoFactorSetup.secret, code: twoFactorSetupCode.trim() }),
      });
      setTwoFactorSetup(null);
      setTwoFactorSetupCode("");
      setTwoFactorStatus("Dvoufázové ověření je zapnuté.");
      const profile = (await api("/auth/me")) as CurrentUser;
      setCurrentUser(profile);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Neplatný ověřovací kód");
    } finally {
      setTwoFactorSetupBusy(false);
    }
  }

  function cancelTwoFactorSetup() {
    setTwoFactorSetup(null);
    setTwoFactorSetupCode("");
  }

  async function disableTwoFactor(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setTwoFactorDisableBusy(true);
    try {
      await api("/auth/2fa/disable", {
        method: "POST",
        body: JSON.stringify({ password: twoFactorDisablePassword, code: twoFactorDisableCode.trim() }),
      });
      setTwoFactorDisableOpen(false);
      setTwoFactorDisablePassword("");
      setTwoFactorDisableCode("");
      setTwoFactorStatus("Dvoufázové ověření bylo vypnuto.");
      const profile = (await api("/auth/me")) as CurrentUser;
      setCurrentUser(profile);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vypnutí 2FA se nezdařilo");
    } finally {
      setTwoFactorDisableBusy(false);
    }
  }

  async function adminResetTwoFactor(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setAdminResetStatus(null);
    setAdminResetBusy(true);
    try {
      const target = adminResetUsername.trim();
      await api(`/users/${encodeURIComponent(target)}/2fa/reset`, { method: "POST" });
      setAdminResetStatus(`2FA pro uživatele „${target}" bylo vypnuto.`);
      setAdminResetUsername("");
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reset 2FA se nezdařil");
    } finally {
      setAdminResetBusy(false);
    }
  }

  async function saveRate(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setWorkingMessage("Ukládám kurz do kurzovního lístku");
    try {
      await api("/rates", {
        method: "POST",
        body: JSON.stringify({
          rate_date: rateDate,
          currency: rateCurrency,
          rate_to_czk: Number(rateValue.replace(",", ".")),
        }),
      });
      setRateValue("");
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kurz se nepodařilo uložit");
    } finally {
      setWorkingMessage(null);
    }
  }

  async function fetchCnbRates() {
    setError(null);
    setWorkingMessage("Načítám denní kurzovní lístek ČNB");
    try {
      await api(`/rates/fetch-cnb?rate_date=${rateDate}`, { method: "POST" });
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kurzovní lístek ČNB se nepodařilo stáhnout");
    } finally {
      setWorkingMessage(null);
    }
  }

  async function saveNotificationSettings(event: React.FormEvent) {
    event.preventDefault();
    setNotifStatus(null);
    setNotifBusy(true);
    try {
      const toNumberOrNull = (value: string) => (value.trim() === "" ? null : Number(value.replace(",", ".")));
      const profile = (await api("/auth/me/notification-settings", {
        method: "PUT",
        body: JSON.stringify({
          alert_daily_change_pct: toNumberOrNull(notifDailyChangePct),
          alert_drop_pct: toNumberOrNull(notifDropPct),
        }),
      })) as CurrentUser;
      setCurrentUser(profile);
      setNotifStatus("Uloženo.");
    } catch (err) {
      setNotifStatus(err instanceof Error ? err.message : "Nastavení se nepodařilo uložit");
    } finally {
      setNotifBusy(false);
    }
  }

  function toggleNewUserAgenda(agendaId: string) {
    setNewUser((value) => {
      const selected = new Set(value.allowed_agendas);
      if (selected.has(agendaId)) selected.delete(agendaId);
      else selected.add(agendaId);
      return { ...value, allowed_agendas: [...selected] };
    });
  }

  async function createUser(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setWorkingMessage("Zakládám nového uživatele a ukládám jeho práva");
    try {
      await api("/users", {
        method: "POST",
        body: JSON.stringify(newUser),
      });
      setNewUser({ username: "", password: "", full_name: "", is_admin: false, allowed_agendas: [] });
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Uživatele se nepodařilo vytvořit");
    } finally {
      setWorkingMessage(null);
    }
  }

  // Refetches /auth/me so currentUser.portfolios (the sidebar switcher's
  // source of truth) picks up a Subjekt an admin just created/renamed -
  // loadAll() alone only refreshes the "users" tab's own /portfolios list,
  // not the caller's own profile.
  async function refreshCurrentUser() {
    try {
      const profile = (await api("/auth/me")) as CurrentUser;
      setCurrentUser(profile);
    } catch {
      // Non-fatal - the switcher just stays stale until the next reload/login.
    }
  }

  async function createPortfolio(event: React.FormEvent) {
    event.preventDefault();
    setPortfolioStatus(null);
    setPortfolioBusy(true);
    try {
      await api("/portfolios", { method: "POST", body: JSON.stringify({ name: newPortfolioName.trim() }) });
      setNewPortfolioName("");
      await Promise.all([loadAll(), refreshCurrentUser()]);
    } catch (err) {
      setPortfolioStatus(err instanceof Error ? err.message : "Subjekt se nepodařilo vytvořit");
    } finally {
      setPortfolioBusy(false);
    }
  }

  function startRenamingPortfolio(portfolio: { id: string; name: string }) {
    setRenamingPortfolioId(portfolio.id);
    setRenamePortfolioName(portfolio.name);
    setPortfolioStatus(null);
  }

  async function saveRenamedPortfolio(event: React.FormEvent) {
    event.preventDefault();
    if (!renamingPortfolioId) return;
    setPortfolioStatus(null);
    setPortfolioBusy(true);
    try {
      await api(`/portfolios/${renamingPortfolioId}`, { method: "PUT", body: JSON.stringify({ name: renamePortfolioName.trim() }) });
      setRenamingPortfolioId(null);
      setRenamePortfolioName("");
      await Promise.all([loadAll(), refreshCurrentUser()]);
    } catch (err) {
      setPortfolioStatus(err instanceof Error ? err.message : "Subjekt se nepodařilo přejmenovat");
    } finally {
      setPortfolioBusy(false);
    }
  }

  function openAccessEditor(row: Row) {
    const username = String(row.username);
    const existing = Array.isArray(row.portfolios) ? (row.portfolios as unknown as PortfolioGrant[]) : [];
    const draft: Record<string, string[]> = {};
    for (const grant of existing) draft[grant.id] = grant.allowed_agendas;
    setAccessDraft(draft);
    setEditingAccessUsername(username);
    setAccessStatus(null);
  }

  function toggleAccessAgenda(portfolioId: string, agendaId: string) {
    setAccessDraft((current) => {
      const selected = new Set(current[portfolioId] || []);
      if (selected.has(agendaId)) selected.delete(agendaId);
      else selected.add(agendaId);
      return { ...current, [portfolioId]: [...selected] };
    });
  }

  async function saveAccessDraft() {
    if (!editingAccessUsername) return;
    setAccessBusy(true);
    setAccessStatus(null);
    try {
      const grants = Object.entries(accessDraft)
        .filter(([, agendas]) => agendas.length > 0)
        .map(([portfolio_id, allowed_agendas]) => ({ portfolio_id, allowed_agendas }));
      await api(`/users/${encodeURIComponent(editingAccessUsername)}/portfolio-access`, {
        method: "PUT",
        body: JSON.stringify({ grants }),
      });
      setEditingAccessUsername(null);
      await loadAll();
    } catch (err) {
      setAccessStatus(err instanceof Error ? err.message : "Přístup se nepodařilo uložit");
    } finally {
      setAccessBusy(false);
    }
  }

  function openUserEditor(row: Row) {
    setEditingUserUsername(String(row.username));
    setEditUserDraft({
      full_name: String(row.full_name || ""),
      is_admin: Boolean(row.is_admin),
      is_active: row.is_active !== false,
      allowed_agendas: Array.isArray(row.allowed_agendas) ? (row.allowed_agendas as unknown as string[]) : [],
      password: "",
    });
    setEditUserStatus(null);
  }

  function toggleEditUserAgenda(agendaId: string) {
    setEditUserDraft((value) => {
      const selected = new Set(value.allowed_agendas);
      if (selected.has(agendaId)) selected.delete(agendaId);
      else selected.add(agendaId);
      return { ...value, allowed_agendas: [...selected] };
    });
  }

  async function saveUserEdit() {
    if (!editingUserUsername) return;
    setEditUserBusy(true);
    setEditUserStatus(null);
    try {
      await api(`/users/${encodeURIComponent(editingUserUsername)}`, {
        method: "PUT",
        body: JSON.stringify({
          full_name: editUserDraft.full_name.trim() || null,
          is_admin: editUserDraft.is_admin,
          is_active: editUserDraft.is_active,
          allowed_agendas: editUserDraft.allowed_agendas,
          password: editUserDraft.password.trim() || null,
        }),
      });
      setEditingUserUsername(null);
      await Promise.all([loadAll(), refreshCurrentUser()]);
    } catch (err) {
      setEditUserStatus(err instanceof Error ? err.message : "Uživatele se nepodařilo uložit");
    } finally {
      setEditUserBusy(false);
    }
  }

  async function cleanupLoanSubtotals() {
    setError(null);
    setLoanCleanupStatus(null);
    setLoanCleanupBusy(true);
    try {
      const result = await api(withPortfolio("/loans/cleanup-imported-subtotals"), { method: "POST" });
      setLoanCleanupStatus(
        result.deleted > 0
          ? `Odstraněno ${result.deleted} importovaných mezisoučtových řádků.`
          : "Žádné importované mezisoučty k odstranění nebyly nalezeny.",
      );
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Vyčištění mezisoučtů se nepodařilo");
    } finally {
      setLoanCleanupBusy(false);
    }
  }

  async function importPatriaTrades() {
    setError(null);
    setStockActionStatus(null);
    setStockBusy(true);
    setWorkingMessage("Zpracovávám zkopírované obchody z Patrie");
    try {
      const result = await api(withPortfolio("/stocks/import-patria"), {
        method: "POST",
        body: JSON.stringify({ text: patriaText }),
      });
      setPatriaText("");
      setStockActionStatus(
        `Patria import: načteno ${result.parsed}, uloženo ${result.inserted}, duplicit přeskočeno ${result.skipped_duplicates}.`,
      );
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import Patria se nepodařilo uložit");
    } finally {
      setStockBusy(false);
      setWorkingMessage(null);
    }
  }

  async function refreshStockPrices() {
    setError(null);
    setStockActionStatus(null);
    setStockBusy(true);
    setWorkingMessage("Zjišťuji aktuální ceny titulů přes Yahoo Finance");
    try {
      const result = await api(withPortfolio("/stocks/refresh-prices"), { method: "POST" });
      const errorCount = Array.isArray(result.errors) ? result.errors.length : 0;
      const moverCount = Array.isArray(result.movers) ? result.movers.length : 0;
      setStockActionStatus(
        `Aktualizace cen: upraveno ${result.updated} titulů${errorCount ? `, chyb ${errorCount}` : ""}${
          moverCount ? `, výrazný pohyb u ${moverCount} titulů` : ""
        }.`,
      );
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Aktualizace cen se nepodařila");
    } finally {
      setStockBusy(false);
      setWorkingMessage(null);
    }
  }

  async function recalculateStockData(dryRun = false) {
    setError(null);
    setStockActionStatus(null);
    setStockBusy(true);
    const fromDate = recalcFromDate.trim();
    setWorkingMessage(
      (dryRun
        ? "Počítám kontrolní náhled portfolia a denní statistiky"
        : fromDate
          ? `Přepočítávám portfolio a denní statistiku od ${fromDate}`
          : "Přepočítávám portfolio a celou historii denní statistiky") +
        " - stahují se historické ceny akcií z Yahoo Finance, u větších portfolií to může trvat i přes minutu",
    );
    try {
      const params = new URLSearchParams({ dry_run: dryRun ? "true" : "false" });
      if (fromDate) params.set("date_from", fromDate);
      const result = await api(withPortfolio(`/stocks/recalculate?${params.toString()}`), { method: "POST" });
      const zeroStatsHint =
        result.daily_statistics === 0 && result.date_from
          ? result.computed_range_from && result.computed_range_to
            ? ` Zadané datum ${result.date_from} leží mimo počítaný rozsah (${result.computed_range_from} až ${result.computed_range_to}) - proto se nepřepočetl žádný den. Pro plný přepočet pole vyprázdni.`
            : " Zadané datum leží až po posledním dni, který má být napočítán (dnešek nebo poslední nákup) - proto se nepřepočetl žádný den. Pro plný přepočet pole vyprázdni."
          : "";
      const priceFailureHint =
        result.price_fetch_failures > 0
          ? ` Pozor: pro ${result.price_fetch_failures} akcií se nepodařilo stáhnout historii cen z Yahoo Finance (${result.price_fetch_failed_tickers?.join(", ")}${result.price_fetch_failures > (result.price_fetch_failed_tickers?.length ?? 0) ? ", ..." : ""}) - jejich tržní hodnota a nerealizovaný zisk proto v daných dnech chybí nebo jsou nižší, než ve skutečnosti jsou. Zkus přepočet zopakovat později.`
          : "";
      setStockActionStatus(
        `${dryRun ? "Kontrolní přepočet" : "Přepočet"}${result.date_from ? ` od ${result.date_from}` : " (celá historie)"}: transakce ${result.transactions}, portfolio ${result.portfolio_positions}, statistiky ${result.daily_statistics}.${zeroStatsHint}${priceFailureHint}`,
      );
      if (!dryRun) await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Přepočet akcií se nepodařil");
    } finally {
      setStockBusy(false);
      setWorkingMessage(null);
    }
  }

  async function loadTickerHistory(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setHistoryBusy(true);
    try {
      const params = new URLSearchParams({
        ticker: historyTicker.trim().toUpperCase(),
        date_from: historyFrom,
        date_to: historyTo,
      });
      const result = await api(withPortfolio(`/stocks/ticker-history?${params.toString()}`));
      setHistoryResult(result as TickerHistoryResult);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Historii tickeru se nepodařilo načíst");
    } finally {
      setHistoryBusy(false);
    }
  }

  if (!token && pendingToken) {
    return (
      <main className="login-shell">
        <form className="login-panel" onSubmit={submitTwoFactorLogin}>
          <div className="brand-mark">
            <Lock size={24} />
          </div>
          <h1>Ověřovací kód</h1>
          <p>Zadejte 6místný kód z vaší aplikace pro dvoufázové ověření (Google Authenticator, Authy, …).</p>
          <label>
            Kód
            <input
              value={twoFactorCode}
              onChange={(event) => setTwoFactorCode(event.target.value)}
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus
            />
          </label>
          {error && <p className="error">{error}</p>}
          <button type="submit" disabled={twoFactorBusy || !twoFactorCode.trim()}>
            {twoFactorBusy ? "Ověřuji…" : "Potvrdit"}
          </button>
          <button type="button" className="link-button" onClick={cancelTwoFactorLogin}>
            Zpět na přihlášení
          </button>
        </form>
      </main>
    );
  }

  if (!token) {
    return (
      <main className="login-shell">
        <form className="login-panel" onSubmit={login}>
          <div className="brand-mark">
            <Lock size={24} />
          </div>
          <h1>FinanceSEMA</h1>
          <label>
            Uživatel
            <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
          </label>
          <label>
            Heslo
            <input
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              type="password"
              autoComplete="current-password"
            />
          </label>
          {error && <p className="error">{error}</p>}
          <button type="submit">Přihlásit</button>
        </form>
      </main>
    );
  }

  return (
    <main className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${mobileMenuOpen ? "mobile-menu-open" : ""}`}>
      <button className="mobile-menu-backdrop" onClick={() => setMobileMenuOpen(false)} aria-label="Zavřít menu" />
      <aside className="sidebar">
        <div className="sidebar-head">
          <div className="app-title">FinanceSEMA</div>
          <button
            className="nav-toggle desktop-nav-toggle"
            onClick={() => setSidebarCollapsed((value) => !value)}
            title={sidebarCollapsed ? "Rozbalit menu" : "Sbalit menu"}
            aria-label={sidebarCollapsed ? "Rozbalit menu" : "Sbalit menu"}
          >
            {sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
          </button>
          <button className="nav-toggle mobile-close" onClick={() => setMobileMenuOpen(false)} title="Zavřít menu" aria-label="Zavřít menu">
            <X size={18} />
          </button>
        </div>
        {currentUser && currentUser.portfolios.length > 0 && (
          <div className="sidebar-portfolio">
            <span className="sidebar-portfolio-label">Subjekt</span>
            {currentUser.portfolios.length > 1 ? (
              <select value={activePortfolioId ?? ""} onChange={(event) => setActivePortfolioId(event.target.value)}>
                {currentUser.portfolios.map((portfolio) => (
                  <option key={portfolio.id} value={portfolio.id}>
                    {portfolio.name}
                  </option>
                ))}
              </select>
            ) : (
              <strong>{currentUser.portfolios[0].name}</strong>
            )}
          </div>
        )}
        <nav>
          {visibleNavGroups.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-group-label">{group.label}</div>
              {group.items.map((tabId) => {
                const tab = tabsById[tabId];
                const Icon = tab.icon;
                return (
                  <button
                    key={tab.id}
                    className={tab.id === activeTab ? "active" : ""}
                    onClick={() => {
                      setActiveTab(tab.id);
                      setMobileMenuOpen(false);
                    }}
                    title={tab.label}
                    aria-label={tab.label}
                  >
                    <Icon size={18} />
                    <span>{tab.label}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="sidebar-user">{currentUser?.username || username}</span>
          <button className="sidebar-logout-button" onClick={logout} title="Odhlásit se" aria-label="Odhlásit se">
            <LogOut size={18} />
            <span>Odhlásit se</span>
          </button>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div className="topbar-title">
            <button className="mobile-menu-button" onClick={() => setMobileMenuOpen(true)} title="Otevřít menu" aria-label="Otevřít menu">
              <Menu size={20} />
            </button>
            <div>
              <h1>{active.label}</h1>
              <p>{summary ? `${rows.length} záznamů v aktuálním pohledu` : "Čekám na importovaná data"}</p>
            </div>
          </div>
          <div className="topbar-actions">
            <button className="icon-button" onClick={loadAll} title="Obnovit data">
              <RefreshCw size={18} />
            </button>
          </div>
        </header>

        {summary && (
          <section className="kpis">
            <div>
              <span>Portfolio</span>
              <strong>{money(summary.portfolio_value_czk)}</strong>
            </div>
            <div>
              <span>Zisk portfolia</span>
              <strong className={summary.portfolio_profit_czk >= 0 ? "positive" : "negative"}>{money(summary.portfolio_profit_czk)}</strong>
            </div>
            <div>
              <span>Majetek</span>
              <strong>{money(summary.assets_total)}</strong>
            </div>
            <div>
              <span>Náklady</span>
              <strong>{money(summary.asset_costs_total)}</strong>
            </div>
          </section>
        )}

        {error && <div className="notice">{error}</div>}

        {workingMessage && (
          <section className="progress-panel" aria-live="polite">
            <div className="progress-copy">
              <strong>Probíhá výpočet</strong>
              <span>{workingMessage}</span>
            </div>
            <div className="thermometer" role="progressbar" aria-label={workingMessage}>
              <div />
            </div>
          </section>
        )}

        {activeTab === "loans" && (
          <section className="work-panel compact-panel">
            <div className="panel-header">
              <div>
                <h2>Půjčky</h2>
                <p>
                  Součty po měsících a letech jsou počítané appkou (klikni na řádek pro rozbalení/sbalení) - ne
                  naimportované z Excelu.
                  {loanCleanupStatus ? ` ${loanCleanupStatus}` : ""}
                </p>
              </div>
              <button className="action-button" onClick={cleanupLoanSubtotals} disabled={loanCleanupBusy}>
                <span>{loanCleanupBusy ? "Čistím…" : "Vyčistit staré importované mezisoučty"}</span>
              </button>
            </div>
          </section>
        )}

        {activeTab === "costs" && (
          <section className="work-panel compact-panel">
            <div className="panel-header">
              <div>
                <h2>Náklady podle investice</h2>
                <p>{showCostDetail ? "Zobrazené jsou měsíční mezisoučty i detailní položky." : "Zobrazené jsou pouze měsíční mezisoučty."}</p>
              </div>
              <button className="action-button" onClick={() => setShowCostDetail((value) => !value)}>
                <span>{showCostDetail ? "Skrýt detail" : "Zobrazit detail"}</span>
              </button>
            </div>
            <div className="filter-row">
              <label>
                Investice
                <select value={costAssetFilter} onChange={(event) => setCostAssetFilter(event.target.value)}>
                  <option value="">Všechny investice</option>
                  {costAssets.map((asset) => (
                    <option value={asset} key={asset}>
                      {asset}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </section>
        )}

        {activeTab === "users" && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Uživatelé</h2>
                <p>Správa přístupů k jednotlivým agendám aplikace.</p>
              </div>
            </div>
            <div className="user-settings">
              <div>
                <span>Přihlášený uživatel</span>
                <strong>{currentUser?.username || username}</strong>
              </div>
              <div>
                <span>Stav</span>
                <strong>{currentUser?.is_active === false ? "Neaktivní" : "Přihlášen"}</strong>
              </div>
              <div>
                <span>Role</span>
                <strong>{currentUser?.is_admin ? "Administrátor" : "Uživatel"}</strong>
              </div>
            </div>

            <div className="security-panel">
              <div className="panel-header">
                <div>
                  <h3>Dvoufázové ověření (2FA)</h3>
                  <p>
                    {currentUser?.totp_enabled
                      ? "Zapnuto - při přihlášení bude vyžadován i kód z ověřovací aplikace."
                      : "Vypnuto - doporučeno zapnout před nasazením do produkce, aplikace obsahuje citlivá finanční data."}
                  </p>
                </div>
                {!currentUser?.totp_enabled && !twoFactorSetup && (
                  <button className="action-button" onClick={startTwoFactorSetup} disabled={twoFactorSetupBusy}>
                    <Lock size={16} />
                    <span>{twoFactorSetupBusy ? "Zakládám…" : "Zapnout 2FA"}</span>
                  </button>
                )}
                {currentUser?.totp_enabled && !twoFactorDisableOpen && (
                  <button className="action-button" onClick={() => setTwoFactorDisableOpen(true)}>
                    <span>Vypnout 2FA</span>
                  </button>
                )}
              </div>
              {twoFactorStatus && <div className="success-notice">{twoFactorStatus}</div>}

              {twoFactorSetup && (
                <form className="two-factor-setup" onSubmit={confirmTwoFactorSetup}>
                  <p>Naskenujte QR kód aplikací pro dvoufázové ověření (Google Authenticator, Authy, 1Password, …):</p>
                  <img
                    className="two-factor-qr"
                    src={`data:image/png;base64,${twoFactorSetup.qr_code_png_base64}`}
                    alt="QR kód pro nastavení 2FA"
                  />
                  <p>
                    Nebo zadejte kód ručně: <code>{twoFactorSetup.secret}</code>
                  </p>
                  <label>
                    Ověřovací kód z aplikace
                    <input
                      value={twoFactorSetupCode}
                      onChange={(event) => setTwoFactorSetupCode(event.target.value)}
                      inputMode="numeric"
                      autoComplete="one-time-code"
                    />
                  </label>
                  <div className="stock-actions">
                    <button className="action-button" type="submit" disabled={twoFactorSetupBusy || !twoFactorSetupCode.trim()}>
                      <Save size={16} />
                      <span>Potvrdit a zapnout</span>
                    </button>
                    <button type="button" className="link-button" onClick={cancelTwoFactorSetup}>
                      Zrušit
                    </button>
                  </div>
                </form>
              )}

              {twoFactorDisableOpen && (
                <form className="two-factor-setup" onSubmit={disableTwoFactor}>
                  <label>
                    Heslo
                    <input
                      value={twoFactorDisablePassword}
                      onChange={(event) => setTwoFactorDisablePassword(event.target.value)}
                      type="password"
                      autoComplete="current-password"
                    />
                  </label>
                  <label>
                    Aktuální ověřovací kód
                    <input
                      value={twoFactorDisableCode}
                      onChange={(event) => setTwoFactorDisableCode(event.target.value)}
                      inputMode="numeric"
                      autoComplete="one-time-code"
                    />
                  </label>
                  <div className="stock-actions">
                    <button
                      className="action-button"
                      type="submit"
                      disabled={twoFactorDisableBusy || !twoFactorDisablePassword || !twoFactorDisableCode.trim()}
                    >
                      <span>Vypnout</span>
                    </button>
                    <button type="button" className="link-button" onClick={() => setTwoFactorDisableOpen(false)}>
                      Zrušit
                    </button>
                  </div>
                </form>
              )}

              {currentUser?.is_admin && (
                <form className="two-factor-setup admin-2fa-reset" onSubmit={adminResetTwoFactor}>
                  <p>Uživatel ztratil zařízení s ověřovací aplikací? Administrátor mu může 2FA vypnout (uživatel si ho pak znovu nastaví).</p>
                  <label>
                    Uživatelské jméno
                    <input value={adminResetUsername} onChange={(event) => setAdminResetUsername(event.target.value)} />
                  </label>
                  <button className="action-button" type="submit" disabled={adminResetBusy || !adminResetUsername.trim()}>
                    <span>{adminResetBusy ? "Resetuji…" : "Resetovat 2FA"}</span>
                  </button>
                  {adminResetStatus && <div className="success-notice">{adminResetStatus}</div>}
                </form>
              )}
            </div>

            {currentUser?.is_admin && (
              <form className="user-form" onSubmit={createUser}>
                <label>
                  Uživatelské jméno
                  <input value={newUser.username} onChange={(event) => setNewUser((value) => ({ ...value, username: event.target.value }))} />
                </label>
                <label>
                  Jméno
                  <input value={newUser.full_name} onChange={(event) => setNewUser((value) => ({ ...value, full_name: event.target.value }))} />
                </label>
                <label>
                  Heslo
                  <input
                    value={newUser.password}
                    onChange={(event) => setNewUser((value) => ({ ...value, password: event.target.value }))}
                    type="password"
                    autoComplete="new-password"
                  />
                </label>
                <label className="checkbox-row">
                  <input
                    checked={newUser.is_admin}
                    onChange={(event) => setNewUser((value) => ({ ...value, is_admin: event.target.checked }))}
                    type="checkbox"
                  />
                  Administrátor
                </label>
                {!newUser.is_admin && (
                  <div className="permissions-grid">
                    {tabs
                      .filter((tab) => GLOBAL_AGENDAS.has(tab.id))
                      .map((tab) => (
                        <label className="checkbox-row" key={tab.id}>
                          <input
                            checked={newUser.allowed_agendas.includes(tab.id)}
                            onChange={() => toggleNewUserAgenda(tab.id)}
                            type="checkbox"
                          />
                          {tab.label}
                        </label>
                      ))}
                  </div>
                )}
                <p className="field-hint">
                  Přístup k jednotlivým subjektům (Majetek, Půjčky, Akcie, …) se přiděluje zvlášť níže, po založení uživatele.
                </p>
                <button className="action-button" disabled={Boolean(workingMessage)} type="submit">
                  <Save size={16} />
                  <span>Přidat uživatele</span>
                </button>
              </form>
            )}

            {currentUser?.is_admin && (
              <div className="security-panel">
                <div className="panel-header">
                  <div>
                    <h3>Správa uživatelů</h3>
                    <p>Upravit jméno, roli a globální oprávnění, nebo pro každý subjekt zvlášť nastavit, které agendy v něm uživatel uvidí.</p>
                  </div>
                </div>
                <div className="portfolio-list">
                  {rows
                    // `rows` briefly holds the previous tab's data while a
                    // freshly triggered loadAll() for "users" is still in
                    // flight (activeTab updates synchronously, the fetch
                    // doesn't) - typeof-guard so that transitional window
                    // renders nothing here instead of bogus placeholder rows
                    // for whatever shape the previous tab's rows had.
                    .filter((row) => typeof row.username === "string")
                    .map((row) => (
                      <div className="portfolio-row" key={String(row.username)}>
                        <span>{String(row.username)}</span>
                        <button type="button" className="link-button" onClick={() => openUserEditor(row)}>
                          Upravit
                        </button>
                        {!row.is_admin && (
                          <button type="button" className="link-button" onClick={() => openAccessEditor(row)}>
                            Spravovat přístup
                          </button>
                        )}
                      </div>
                    ))}
                </div>
                {editingUserUsername && (
                  <div className="access-editor">
                    <p>
                      Úprava uživatele <strong>{editingUserUsername}</strong>:
                    </p>
                    <label>
                      Jméno
                      <input
                        value={editUserDraft.full_name}
                        onChange={(event) => setEditUserDraft((value) => ({ ...value, full_name: event.target.value }))}
                      />
                    </label>
                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={editUserDraft.is_admin}
                        onChange={(event) => setEditUserDraft((value) => ({ ...value, is_admin: event.target.checked }))}
                      />
                      Administrátor
                    </label>
                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={editUserDraft.is_active}
                        onChange={(event) => setEditUserDraft((value) => ({ ...value, is_active: event.target.checked }))}
                      />
                      Aktivní
                    </label>
                    {!editUserDraft.is_admin && (
                      <div className="permissions-grid">
                        {tabs
                          .filter((tab) => GLOBAL_AGENDAS.has(tab.id))
                          .map((tab) => (
                            <label className="checkbox-row" key={tab.id}>
                              <input
                                type="checkbox"
                                checked={editUserDraft.allowed_agendas.includes(tab.id)}
                                onChange={() => toggleEditUserAgenda(tab.id)}
                              />
                              {tab.label}
                            </label>
                          ))}
                      </div>
                    )}
                    <label>
                      Nové heslo
                      <input
                        type="password"
                        value={editUserDraft.password}
                        onChange={(event) => setEditUserDraft((value) => ({ ...value, password: event.target.value }))}
                        placeholder="Ponechat prázdné = beze změny"
                        autoComplete="new-password"
                      />
                    </label>
                    {editUserStatus && <div className="success-notice">{editUserStatus}</div>}
                    <div className="stock-actions">
                      <button className="action-button" onClick={saveUserEdit} disabled={editUserBusy}>
                        <Save size={16} />
                        <span>Uložit uživatele</span>
                      </button>
                      <button type="button" className="link-button" onClick={() => setEditingUserUsername(null)}>
                        Zrušit
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {currentUser?.is_admin && editingAccessUsername && (
              <div className="security-panel">
                <div className="access-editor">
                  <p>
                    Přístup uživatele <strong>{editingAccessUsername}</strong>:
                  </p>
                  {portfolios.map((portfolio) => (
                    <div key={portfolio.id} className="access-editor-portfolio">
                      <h4>{portfolio.name}</h4>
                      <div className="permissions-grid">
                        {PORTFOLIO_SCOPED_TABS.map((tab) => (
                          <label className="checkbox-row" key={tab.id}>
                            <input
                              checked={(accessDraft[portfolio.id] || []).includes(tab.id)}
                              onChange={() => toggleAccessAgenda(portfolio.id, tab.id)}
                              type="checkbox"
                            />
                            {tab.label}
                          </label>
                        ))}
                      </div>
                    </div>
                  ))}
                  {accessStatus && <div className="success-notice">{accessStatus}</div>}
                  <div className="stock-actions">
                    <button className="action-button" onClick={saveAccessDraft} disabled={accessBusy}>
                      <Save size={16} />
                      <span>Uložit přístup</span>
                    </button>
                    <button type="button" className="link-button" onClick={() => setEditingAccessUsername(null)}>
                      Zrušit
                    </button>
                  </div>
                </div>
              </div>
            )}
          </section>
        )}

        {activeTab === "subjects" && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Subjekty</h2>
                <p>Subjekt sdružuje majetek, půjčky i akcie jednoho vlastníka - uživatelům se pak v záložce Uživatelé přiděluje přístup k jednotlivým subjektům a agendám.</p>
              </div>
            </div>
            {portfolioStatus && <div className="success-notice">{portfolioStatus}</div>}
            <div className="portfolio-list">
              {portfolios.map((portfolio) =>
                renamingPortfolioId === portfolio.id ? (
                  <form className="portfolio-row" key={portfolio.id} onSubmit={saveRenamedPortfolio}>
                    <input value={renamePortfolioName} onChange={(event) => setRenamePortfolioName(event.target.value)} autoFocus />
                    <button className="action-button" type="submit" disabled={portfolioBusy || !renamePortfolioName.trim()}>
                      <Save size={16} />
                    </button>
                    <button type="button" className="link-button" onClick={() => setRenamingPortfolioId(null)}>
                      Zrušit
                    </button>
                  </form>
                ) : (
                  <div className="portfolio-row" key={portfolio.id}>
                    <span>{portfolio.name}</span>
                    <button type="button" className="link-button" onClick={() => startRenamingPortfolio(portfolio)}>
                      Přejmenovat
                    </button>
                  </div>
                ),
              )}
            </div>
            <form className="rate-form" onSubmit={createPortfolio}>
              <label>
                Nový subjekt
                <input value={newPortfolioName} onChange={(event) => setNewPortfolioName(event.target.value)} placeholder="Např. Martin Ševčík" />
              </label>
              <button className="action-button" type="submit" disabled={portfolioBusy || !newPortfolioName.trim()}>
                <Save size={16} />
                <span>Vytvořit subjekt</span>
              </button>
            </form>
          </section>
        )}

        {activeTab === "rates" && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Kurzovní lístek</h2>
                <p>{latestRates?.rate_date ? `Poslední importované datum: ${latestRates.rate_date}` : "Zatím nejsou načtené žádné kurzy"}</p>
              </div>
              <button className="action-button" onClick={fetchCnbRates} disabled={Boolean(workingMessage)}>
                <Download size={16} />
                <span>Načíst ČNB</span>
              </button>
            </div>
            <form className="rate-form" onSubmit={saveRate}>
              <label>
                Datum
                <input value={rateDate} onChange={(event) => setRateDate(event.target.value)} type="date" />
              </label>
              <label>
                Měna
                <input value={rateCurrency} onChange={(event) => setRateCurrency(event.target.value.toUpperCase())} />
              </label>
              <label>
                Kurz CZK
                <input value={rateValue} onChange={(event) => setRateValue(event.target.value)} inputMode="decimal" placeholder="24,245" />
              </label>
              <button className="action-button" type="submit" disabled={Boolean(workingMessage)}>
                <Save size={16} />
                <span>Uložit kurz</span>
              </button>
            </form>
            <div className="latest-rate-row">
              <div>
                <span>Datum</span>
                <strong>{latestRates?.rate_date || ""}</strong>
              </div>
              <div>
                <span>EUR</span>
                <strong>{formatValue("rate_to_czk", rateForCurrency(latestRates, "EUR"))}</strong>
              </div>
              <div>
                <span>USD</span>
                <strong>{formatValue("rate_to_czk", rateForCurrency(latestRates, "USD"))}</strong>
              </div>
            </div>
          </section>
        )}

        {activeTab === "settings" && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Nastavení upozornění</h2>
                <p>
                  Osobní prahy pro upozornění na záložce Upozornění a při přepočtu akcií. Prázdné pole = použije se
                  výchozí hodnota (10 %).
                </p>
              </div>
            </div>
            <form className="rate-form" onSubmit={saveNotificationSettings}>
              <label>
                Denní změna (%) pro upozornění
                <input
                  value={notifDailyChangePct}
                  onChange={(event) => setNotifDailyChangePct(event.target.value)}
                  inputMode="decimal"
                  placeholder="10"
                />
              </label>
              <label>
                Pokles portfolia (%) pro upozornění
                <input
                  value={notifDropPct}
                  onChange={(event) => setNotifDropPct(event.target.value)}
                  inputMode="decimal"
                  placeholder="10"
                />
              </label>
              <button className="action-button" type="submit" disabled={notifBusy}>
                <Save size={16} />
                <span>Uložit</span>
              </button>
            </form>
            {notifStatus && <div className="success-notice">{notifStatus}</div>}
          </section>
        )}

        {activeTab === "alerts" && alerts && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Upozornění</h2>
                <p>
                  Denní pohyby{alerts.daily_movers_as_of ? ` (k ${alerts.daily_movers_as_of})` : ""}, sledované tituly pod
                  limitní cenou a pozice s poklesem nad {alerts.threshold_pct} %. Prahy pro upozornění nastavíte v záložce Nastavení.
                </p>
              </div>
            </div>
            {alerts.daily_movers.length === 0 &&
            alerts.watchlist_limit_breaches.length === 0 &&
            alerts.portfolio_drawdowns.length === 0 ? (
              <p className="alert-empty">Žádná aktivní upozornění.</p>
            ) : (
              <div className="mini-grids">
                <div>
                  <h3>Denní pohyby</h3>
                  {alerts.daily_movers.length === 0 && (
                    <p>
                      <span>Žádné</span>
                    </p>
                  )}
                  {alerts.daily_movers.map((row) => (
                    <p key={`mover-${String(row.ticker)}`}>
                      <span>{String(row.ticker)}</span>
                      <strong className={numberValue(row.change_pct) >= 0 ? "positive" : "negative"}>
                        {formatSignedProfit(numberValue(row.change_pct))}
                      </strong>
                    </p>
                  ))}
                </div>
                <div>
                  <h3>Sledované pod limitem</h3>
                  {alerts.watchlist_limit_breaches.length === 0 && (
                    <p>
                      <span>Žádné</span>
                    </p>
                  )}
                  {alerts.watchlist_limit_breaches.map((row) => (
                    <p key={String(row.ticker)}>
                      <span>{String(row.ticker)}</span>
                      <strong>
                        {formatValue("current_price", row.current_price)} / limit {formatValue("limit_price", row.limit_price)}
                      </strong>
                    </p>
                  ))}
                </div>
                <div>
                  <h3>Propady portfolia</h3>
                  {alerts.portfolio_drawdowns.length === 0 && (
                    <p>
                      <span>Žádné</span>
                    </p>
                  )}
                  {alerts.portfolio_drawdowns.map((row) => (
                    <p key={String(row.ticker)}>
                      <span>{String(row.ticker)}</span>
                      <strong className="negative">{formatValue("profit_pct", row.profit_pct)}</strong>
                    </p>
                  ))}
                </div>
              </div>
            )}
          </section>
        )}

        {activeTab === "history" && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Sledování akcie</h2>
                <p>Denní kurzy, nákupy a vyhodnocení zisku/ztráty daného titulu za zvolené období.</p>
              </div>
            </div>
              <form className="history-form" onSubmit={loadTickerHistory}>
                <label>
                  Ticker
                  <input
                    value={historyTicker}
                    onChange={(event) => setHistoryTicker(event.target.value.toUpperCase())}
                    placeholder="AAPL, DBK.DE, …"
                  />
                </label>
                <label>
                  Od
                  <input type="date" value={historyFrom} onChange={(event) => setHistoryFrom(event.target.value)} />
                </label>
                <label>
                  Do
                  <input type="date" value={historyTo} onChange={(event) => setHistoryTo(event.target.value)} />
                </label>
                <button className="action-button" type="submit" disabled={historyBusy || !historyTicker.trim()}>
                  <LineChart size={16} />
                  <span>Historie tickeru</span>
                </button>
              </form>
            {historyResult && (
              <div className="history-block">
                {historyResult.rows.length === 0 ? (
                  <p className="alert-empty">Pro zadaný ticker a období nejsou k dispozici žádná data.</p>
                ) : (
                  <>
                    {historyResult.summary && (
                      <div className="history-summary">
                        <div>
                          <span>Ticker</span>
                          <strong>
                            {historyResult.ticker} ({historyResult.currency})
                          </strong>
                        </div>
                        <div>
                          <span>Držených ks</span>
                          <strong>{formatHistoryValue("cumulative_quantity", historyResult.summary.cumulative_quantity)}</strong>
                        </div>
                        <div>
                          <span>Hodnota CZK</span>
                          <strong>{formatHistoryValue("value_czk", historyResult.summary.value_czk)}</strong>
                        </div>
                        <div>
                          <span>Zisk/Ztr. CZK</span>
                          <strong
                            className={numberValue(historyResult.summary.profit_czk) >= 0 ? "positive" : "negative"}
                          >
                            {formatSignedProfitPrecise(numberValue(historyResult.summary.profit_czk))}
                          </strong>
                        </div>
                      </div>
                    )}
                    <div className="nested-table">
                      <table>
                        <thead>
                          <tr>
                            {historyColumns.map((col) => (
                              <th key={col}>{labels[col] || col}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {historyResult.rows.map((row, index) => (
                            <tr key={String(row.trade_date || index)}>
                              {historyColumns.map((col) => {
                                const raw = row[col];
                                const isSigned = HISTORY_SIGNED_COLUMNS.has(col) && typeof raw === "number";
                                return (
                                  <td className={isNumericCell(raw) ? "numeric-cell" : ""} key={col}>
                                    {isSigned ? (
                                      <span className={numberValue(raw) >= 0 ? "positive" : "negative"}>
                                        {formatSignedProfitPrecise(numberValue(raw), col === "change_pct")}
                                      </span>
                                    ) : (
                                      formatHistoryValue(col, raw)
                                    )}
                                  </td>
                                );
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </div>
            )}
          </section>
        )}

        {(STOCK_OVERVIEW_TABS.includes(activeTab) || activeTab === "stats") && (
          <section className="work-panel compact-panel">
            <div className="panel-header">
              <div>
                <h2>Přepočet portfolia</h2>
                <p>Přepočítá portfolio a denní statistiku ze všech akciových transakcí.</p>
              </div>
              <div className="stock-actions">
                <button className="action-button" onClick={refreshStockPrices} disabled={stockBusy}>
                  <RefreshCw size={16} />
                  <span>Ceny</span>
                </button>
                <RecalcFromField value={recalcFromDate} onChange={setRecalcFromDate} />
                <button
                  className="action-button"
                  onClick={() => recalculateStockData(true)}
                  disabled={stockBusy}
                  title="Jen náhled počtů - nic se neuloží do databáze"
                >
                  <Calculator size={16} />
                  <span>Kontrola (náhled)</span>
                </button>
                <button className="action-button" onClick={() => recalculateStockData(false)} disabled={stockBusy}>
                  <Calculator size={16} />
                  <span>Přepočítat</span>
                </button>
              </div>
            </div>
            {stockActionStatus && <div className="success-notice">{stockActionStatus}</div>}
          </section>
        )}

        {STOCK_OVERVIEW_TABS.includes(activeTab) && stockOverview && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Akciový souhrn</h2>
                <p>Přehled je zatím počítaný z importovaných excelových dat.</p>
              </div>
            </div>
            {activeTab === "transactions" && (
              <div className="patria-import">
                <label>
                  Import Patria
                  <textarea
                    value={patriaText}
                    onChange={(event) => setPatriaText(event.target.value)}
                    placeholder="Vložte zkopírovaný dvouřádkový export obchodů z Patrie"
                  />
                </label>
                <button className="action-button" onClick={importPatriaTrades} disabled={stockBusy || !patriaText.trim()}>
                  <Download size={16} />
                  <span>Importovat Patria</span>
                </button>
              </div>
            )}
            <div className="mini-grids">
              <div>
                <h3>Pohyby</h3>
                {stockOverview.movements.map((row) => (
                  <p key={String(row.movement_type)}>
                    <span>{String(row.movement_type || "Bez typu")}</span>
                    <strong>{formatValue("count", row.count)}</strong>
                  </p>
                ))}
              </div>
              <div>
                <h3>Měny</h3>
                <div className="currency-summary-header">
                  <span>Měna</span>
                  <span>Cizí měna</span>
                  <span>CZK</span>
                </div>
                {stockOverview.currencies.map((row) => (
                  <p className="currency-summary-row" key={String(row.currency)}>
                    <span>{String(row.currency || "Bez měny")}</span>
                    <strong>{formatValue("amount_ccy", row.amount_ccy)}</strong>
                    <strong>{formatValue("amount_czk", row.amount_czk)}</strong>
                  </p>
                ))}
              </div>
            </div>
          </section>
        )}

        {activeTab === "charts" && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Grafy portfolia</h2>
                <p>{chartData.length > 0 ? `Z posledních ${chartData.length} obchodních dnů.` : "Zatím není dost dat pro graf."}</p>
              </div>
            </div>
            {chartData.length === 0 ? (
              <p className="alert-empty">Grafy se zobrazí po prvním přepočtu akciových dat (záložka Transakce).</p>
            ) : (
              <div className="portfolio-charts-grid portfolio-charts-grid-large">
                <PortfolioChart
                  title="Hodnota portfolia vs Investice"
                  mode="lines"
                  data={chartData}
                  series={[
                    { key: "invested_czk", label: "Investice CZK", color: "#2a78d6" },
                    { key: "total_value_czk", label: "Hodnota CZK", color: "#eb6834" },
                  ]}
                  formatValue={money}
                  large
                />
                <PortfolioChart
                  title="Nerealizovaný zisk / ztráta (CZK)"
                  mode="diverging-area"
                  data={chartData}
                  series={[{ key: "unrealized_profit_czk", label: "Nereal. zisk", color: "var(--accent)" }]}
                  formatValue={money}
                  large
                />
                <PortfolioChart
                  title="Dividendy kumulativně"
                  mode="area"
                  data={chartData}
                  series={[{ key: "dividends_total", label: "Dividendy", color: "#1baf7a" }]}
                  formatValue={money}
                  large
                />
              </div>
            )}
          </section>
        )}

        {activeTab === "assets" && assetAgendas.length > 0 && (
          <section className="asset-agendas">
            <div className="section-heading">
              <h2>Majetkové agendy</h2>
              <p>Každý majetek má vlastní přehled hodnot, součty nákladů a navázané položky.</p>
            </div>
            {assetAgendas.map((agenda) => (
              <article className="asset-agenda" key={String(agenda.asset.id || agenda.asset.code)}>
                <header>
                  <div>
                    <h2>{String(agenda.asset.name || "")}</h2>
                    <p>
                      {String(agenda.asset.code || "")} · {String(agenda.asset.owner || "Bez vlastníka")} · {String(agenda.asset.asset_type || "Bez typu")}
                    </p>
                  </div>
                  <div className="agenda-metrics">
                    <div>
                      <span>Hodnota</span>
                      <strong>{formatValue("total_value", agenda.asset.total_value)}</strong>
                    </div>
                    <div>
                      <span>Náklady</span>
                      <strong>{formatValue("amount", agenda.cost_total)}</strong>
                    </div>
                    <div>
                      <span>Záznamy</span>
                      <strong>{formatValue("count", agenda.cost_count)}</strong>
                    </div>
                  </div>
                </header>
                {agenda.categories.length > 0 && (
                  <div className="category-strip">
                    {agenda.categories.map((category) => (
                      <div key={String(category.category)}>
                        <span>{String(category.category)}</span>
                        <strong>{formatValue("amount", category.amount)}</strong>
                      </div>
                    ))}
                  </div>
                )}
                {interestPlanEntries(agenda.asset).length > 0 && (
                  <div className="interest-plan">
                    <h3>Dopočtená projekce úroku (2026–2055)</h3>
                    <div className="interest-plan-grid">
                      {interestPlanEntries(agenda.asset).map(([year, amount]) => (
                        <div key={year}>
                          <span>{year}</span>
                          <strong>{formatValue("amount", amount)}</strong>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                <div className="nested-table">
                  <table>
                    <thead>
                      <tr>
                        {costColumns.map((col) => (
                          <th key={col}>{labels[col] || col}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {agenda.costs.map((cost, index) => (
                        <tr key={String(cost.id || index)}>
                          {costColumns.map((col) => (
                            <td className={isNumericCell(cost[col]) ? "numeric-cell" : ""} key={col}>
                              {formatValue(col, cost[col])}
                            </td>
                          ))}
                        </tr>
                      ))}
                      {agenda.costs.length === 0 && (
                        <tr>
                          <td colSpan={costColumns.length}>Bez evidovaných nákladů.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </article>
            ))}
          </section>
        )}

        {!NON_TABLE_TABS.has(activeTab) && (
        <section className={`table-wrap ${activeTab === "stats" ? "stats-table-wrap" : ""}`}>
          <table>
            <thead>
              <tr>
                {(columns[activeTab] || []).map((col) => (
                  <th key={col}>{labels[col] || col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tableRows.map((row, index) => {
                const isStatSummary = activeTab === "stats" && row.row_kind === "summary";
                const isLoanYearSummary = activeTab === "loans" && row.row_kind === "year-summary";
                const isLoanMonthSummary = activeTab === "loans" && row.row_kind === "month-summary";
                const isClickableSummary = isStatSummary || isLoanYearSummary || isLoanMonthSummary;
                const monthKey = String(row.month_key || "");
                const yearKey = String(row.year_key || "");
                const handleRowClick = isStatSummary
                  ? () => toggleStatMonth(monthKey)
                  : isLoanYearSummary
                    ? () => toggleLoanYear(yearKey)
                    : isLoanMonthSummary
                      ? () => toggleLoanMonth(monthKey)
                      : undefined;
                const summaryExpanded = isStatSummary
                  ? expandedStatMonths.has(monthKey)
                  : isLoanYearSummary
                    ? expandedLoanYears.has(yearKey)
                    : isLoanMonthSummary
                      ? expandedLoanMonths.has(monthKey)
                      : false;
                return (
                  <tr
                    className={`${String(row.row_kind || "")}${isClickableSummary ? " clickable-row" : ""}`}
                    key={String(row.id || row.ticker || row.stat_date || row.year_key || row.month_key || row.period_label || index)}
                    onClick={handleRowClick}
                  >
                    {(columns[activeTab] || []).map((col, colIndex) => (
                      <td className={isNumericCell(row[col]) ? "numeric-cell" : ""} key={col}>
                        {isClickableSummary && colIndex === 0 ? (
                          <span className={`stat-month-toggle${isLoanMonthSummary ? " nested-toggle" : ""}`}>
                            {summaryExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                            {formatValue(col, row[col])}
                          </span>
                        ) : signedProfitColumns.has(col) && typeof row[col] === "number" ? (
                          <span className={numberValue(row[col]) >= 0 ? "positive" : "negative"}>
                            {formatSignedProfit(numberValue(row[col]))}
                          </span>
                        ) : activeTab === "stats" ? (
                          formatStatValue(col, row[col])
                        ) : (
                          formatValue(col, row[col])
                        )}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
        )}
      </section>
    </main>
  );
}
