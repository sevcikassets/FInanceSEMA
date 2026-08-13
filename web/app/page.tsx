"use client";

import {
  Building2,
  Calculator,
  CalendarDays,
  Coins,
  Download,
  FileSpreadsheet,
  ListChecks,
  LineChart,
  Lock,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Save,
  ShieldCheck,
  WalletCards,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

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

type CurrentUser = {
  username: string;
  full_name: string | null;
  is_active: boolean;
  is_admin: boolean;
  allowed_agendas: string[];
};

const tabs = [
  { id: "assets", label: "Majetek", icon: Building2, endpoint: "/assets" },
  { id: "costs", label: "Náklady", icon: WalletCards, endpoint: "/assets/costs" },
  { id: "loans", label: "Půjčky", icon: Coins, endpoint: "/loans/movements" },
  { id: "transactions", label: "Transakce", icon: FileSpreadsheet, endpoint: "/stocks/transactions" },
  { id: "watchlist", label: "Sledované", icon: ListChecks, endpoint: "/stocks/watchlist" },
  { id: "stats", label: "Denní statistika", icon: ShieldCheck, endpoint: "/stocks/statistics" },
  { id: "portfolio", label: "Portfolio", icon: LineChart, endpoint: "/stocks/portfolio" },
  { id: "rates", label: "Denní kurzy", icon: CalendarDays, endpoint: "/rates/daily?limit=500" },
  { id: "users", label: "Uživatelé", icon: Lock, endpoint: "/users" },
];

const navGroups = [
  { label: "Majetek", items: ["assets", "costs"] },
  { label: "Půjčky", items: ["loans"] },
  { label: "Akcie", items: ["transactions", "watchlist", "stats", "portfolio"] },
  { label: "Nastavení", items: ["rates", "users"] },
];

const tabsById = Object.fromEntries(tabs.map((tab) => [tab.id, tab]));

const columns: Record<string, string[]> = {
  portfolio: ["ticker", "name", "quantity", "currency", "market_value_czk", "invested_czk", "profit_czk", "profit_pct"],
  assets: ["code", "owner", "asset_type", "name", "total_value", "own_funds", "borrowed_amount", "interest_rate"],
  costs: ["cost_date", "asset", "payer", "supplier", "category", "item", "amount"],
  loans: ["movement_date", "period_label", "lender", "borrower", "amount", "interest_rate", "planned_end_date", "description"],
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
  users: ["username", "full_name", "is_admin", "is_active", "allowed_agendas"],
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

function interestPlanEntries(asset: Row): [string, number][] {
  const plan = asset.computed_interest_plan;
  if (!plan || typeof plan !== "object" || Array.isArray(plan)) return [];
  return Object.entries(plan as Record<string, unknown>)
    .filter((entry): entry is [string, number] => typeof entry[1] === "number")
    .sort((a, b) => a[0].localeCompare(b[0]));
}

function formatValue(key: string, value: Row[string]) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "boolean") return value ? "ano" : "ne";
  if (Array.isArray(value)) return value.map((item) => tabsById[String(item)]?.label || String(item)).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "number") {
    if (key.includes("pct") || (key.includes("rate") && key !== "rate_to_czk")) {
      return new Intl.NumberFormat("cs-CZ", { style: "percent", maximumFractionDigits: 2 }).format(value);
    }
    return new Intl.NumberFormat("cs-CZ", { maximumFractionDigits: 2 }).format(value);
  }
  return String(value);
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

function buildStatisticRows(rows: Row[], showDetail: boolean) {
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
    const summary: Row = {
      row_kind: "summary",
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

    if (showDetail) {
      for (const row of sortedRows) {
        result.push({ ...row, row_kind: "detail", period_label: row.stat_date });
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

export default function Page() {
  const [token, setToken] = useState<string | null>(null);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
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
  const [showStatDetail, setShowStatDetail] = useState(true);
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
  const [workingMessage, setWorkingMessage] = useState<string | null>(null);
  const [newUser, setNewUser] = useState({
    username: "",
    password: "",
    full_name: "",
    is_admin: false,
    allowed_agendas: [] as string[],
  });
  const allowedAgendaSet = useMemo(
    () => new Set(currentUser?.is_admin ? tabs.map((tab) => tab.id) : currentUser?.allowed_agendas || tabs.map((tab) => tab.id)),
    [currentUser],
  );
  const visibleTabs = useMemo(() => tabs.filter((tab) => allowedAgendaSet.has(tab.id)), [allowedAgendaSet]);
  const visibleNavGroups = useMemo(
    () =>
      navGroups
        .map((group) => ({ ...group, items: group.items.filter((item) => allowedAgendaSet.has(item)) }))
        .filter((group) => group.items.length > 0),
    [allowedAgendaSet],
  );
  const active = useMemo(() => tabs.find((tab) => tab.id === activeTab) || visibleTabs[0] || tabs[0], [activeTab, visibleTabs]);
  const costAssets = useMemo(
    () => [...new Set(rows.map((row) => String(row.asset || "")).filter(Boolean))].sort((a, b) => a.localeCompare(b)),
    [rows],
  );
  const tableRows =
    activeTab === "stats"
      ? buildStatisticRows(rows, showStatDetail)
      : activeTab === "costs"
        ? buildCostRows(rows, costAssetFilter, showCostDetail)
        : rows;

  useEffect(() => {
    const saved = localStorage.getItem("finance-token");
    if (saved) setToken(saved);
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

  useEffect(() => {
    if (visibleTabs.length > 0 && !allowedAgendaSet.has(activeTab)) {
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

  async function loadAll() {
    if (!token) return;
    setError(null);
    try {
      const isStockTab = ["transactions", "watchlist", "portfolio", "stats"].includes(activeTab);
      const requests: Promise<unknown>[] = [api("/summary"), api(active.endpoint)];
      if (activeTab === "rates") requests.push(api("/rates/latest"));
      if (isStockTab) requests.push(api("/stocks/overview"), api("/stocks/alerts"));
      if (activeTab === "assets") requests.push(api("/assets/agendas"));
      const [summaryData, rowsData, ...extra] = await Promise.all(requests);
      setSummary(summaryData as Summary);
      setRows(rowsData as Row[]);
      if (activeTab === "rates") setLatestRates(extra[0] as LatestRates);
      if (isStockTab) {
        setStockOverview(extra[0] as StockOverview);
        setAlerts(extra[1] as Alerts);
      }
      if (activeTab === "assets") setAssetAgendas(extra[0] as AssetAgenda[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Nepodařilo se načíst data");
    }
  }

  useEffect(() => {
    loadAll();
  }, [token, activeTab]);

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
      localStorage.setItem("finance-token", data.token);
      setToken(data.token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Přihlášení selhalo");
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

  async function importPatriaTrades() {
    setError(null);
    setStockActionStatus(null);
    setStockBusy(true);
    setWorkingMessage("Zpracovávám zkopírované obchody z Patrie");
    try {
      const result = await api("/stocks/import-patria", {
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
      const result = await api("/stocks/refresh-prices", { method: "POST" });
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
    setWorkingMessage(dryRun ? "Počítám kontrolní náhled portfolia a denní statistiky" : "Přepočítávám portfolio a denní statistiku");
    try {
      const result = await api(`/stocks/recalculate?dry_run=${dryRun ? "true" : "false"}`, { method: "POST" });
      setStockActionStatus(
        `${dryRun ? "Kontrolní přepočet" : "Přepočet"}: transakce ${result.transactions}, portfolio ${result.portfolio_positions}, statistiky ${result.daily_statistics}.`,
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
      const result = await api(`/stocks/ticker-history?${params.toString()}`);
      setHistoryResult(result as TickerHistoryResult);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Historii tickeru se nepodařilo načíst");
    } finally {
      setHistoryBusy(false);
    }
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
          <button className="icon-button" onClick={loadAll} title="Obnovit data">
            <RefreshCw size={18} />
          </button>
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
                    {tabs.map((tab) => (
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
                <button className="action-button" disabled={Boolean(workingMessage)} type="submit">
                  <Save size={16} />
                  <span>Přidat uživatele</span>
                </button>
              </form>
            )}
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

        {["transactions", "watchlist", "portfolio", "stats"].includes(activeTab) && alerts && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Upozornění</h2>
                <p>Sledované tituly pod limitní cenou a pozice s poklesem nad {alerts.threshold_pct} %.</p>
              </div>
            </div>
            {alerts.watchlist_limit_breaches.length === 0 && alerts.portfolio_drawdowns.length === 0 ? (
              <p className="alert-empty">Žádná aktivní upozornění.</p>
            ) : (
              <div className="mini-grids">
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

        {["transactions", "watchlist", "portfolio", "stats"].includes(activeTab) && stockOverview && (
          <section className="work-panel">
            <div className="panel-header">
              <div>
                <h2>Akciový souhrn</h2>
                <p>Přehled je zatím počítaný z importovaných excelových dat.</p>
              </div>
              <div className="stock-actions">
                <button className="action-button" onClick={refreshStockPrices} disabled={stockBusy}>
                  <RefreshCw size={16} />
                  <span>Ceny</span>
                </button>
                <button className="action-button" onClick={() => recalculateStockData(true)} disabled={stockBusy}>
                  <Calculator size={16} />
                  <span>Kontrola</span>
                </button>
                <button className="action-button" onClick={() => recalculateStockData(false)} disabled={stockBusy}>
                  <Calculator size={16} />
                  <span>Přepočítat</span>
                </button>
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
            {activeTab === "transactions" && (
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
            )}
            {activeTab === "transactions" && historyResult && (
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
                          <strong>{formatValue("cumulative_quantity", historyResult.summary.cumulative_quantity)}</strong>
                        </div>
                        <div>
                          <span>Hodnota CZK</span>
                          <strong>{formatValue("value_czk", historyResult.summary.value_czk)}</strong>
                        </div>
                        <div>
                          <span>Zisk/Ztr. CZK</span>
                          <strong
                            className={numberValue(historyResult.summary.profit_czk) >= 0 ? "positive" : "negative"}
                          >
                            {formatValue("profit_czk", historyResult.summary.profit_czk)}
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
                              {historyColumns.map((col) => (
                                <td className={isNumericCell(row[col]) ? "numeric-cell" : ""} key={col}>
                                  {formatValue(col, row[col])}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </div>
            )}
            {stockActionStatus && <div className="success-notice">{stockActionStatus}</div>}
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

        {activeTab === "stats" && (
          <section className="work-panel compact-panel">
            <div className="panel-header">
              <div>
                <h2>Měsíční mezisoučty</h2>
                <p>{showStatDetail ? "Zobrazené jsou měsíce i denní detail." : "Zobrazené jsou pouze měsíční souhrny."}</p>
              </div>
              <button className="action-button" onClick={() => setShowStatDetail((value) => !value)}>
                <span>{showStatDetail ? "Skrýt detail" : "Zobrazit detail"}</span>
              </button>
              <button className="action-button" onClick={() => recalculateStockData(false)} disabled={stockBusy}>
                <Calculator size={16} />
                <span>Napočítat denní statistiky</span>
              </button>
            </div>
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

        {activeTab !== "assets" && (
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
              {tableRows.map((row, index) => (
                <tr className={String(row.row_kind || "")} key={String(row.id || row.ticker || row.stat_date || row.period_label || index)}>
                  {(columns[activeTab] || []).map((col) => (
                    <td className={isNumericCell(row[col]) ? "numeric-cell" : ""} key={col}>
                      {formatValue(col, row[col])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        )}
      </section>
    </main>
  );
}
