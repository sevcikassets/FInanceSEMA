"""E-mailové reporty na Subjekt (Portfolio.report_email/report_period) -
viz run_daily_email_reports v main.py, které tuto sadu funkcí volá jednou
denně a rozhoduje (should_send_today), kterému Subjektu se dnes má report
poslat.

Obsah reportu se škáluje podle zvolené periody, ne podle kalendářního
měsíce natvrdo - "denní plusy a mínusy" u denního reportu znamenají
skutečně dnešek, ne žebříček přes celý měsíc:
  - daily:   okno = dnešek samotný (den vs. předchozí obchodní den)
  - weekly:  okno = posledních 7 kalendářních dní
  - monthly: okno = aktuální měsíc od 1. do dneška (beze změny oproti
             původnímu chování)

Chart se kreslí přes Pillow (už je závislost kvůli QR kódům pro 2FA), aby
report nepotřeboval matplotlib/numpy jen kvůli jednomu jednoduchému
čárovému grafu - a přirozeně se vůbec nevykreslí, když okno má jen jeden
den (denní perioda), protože pro jeden bod srovnávací graf nedává smysl.
"""

from __future__ import annotations

import io
import logging
import smtplib
import uuid
from datetime import date, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import DailyStatistic, Portfolio, PortfolioPosition, SmtpSettings
from .stock_services import fetch_yahoo_history, rate_for_day

logger = logging.getLogger(__name__)

CHART_WIDTH = 860
CHART_HEIGHT = 380
CHART_MARGIN_LEFT = 60
CHART_MARGIN_RIGHT = 20
CHART_MARGIN_TOP = 34
CHART_MARGIN_BOTTOM = 40
COLOR_PORTFOLIO = (42, 120, 214)
COLOR_BENCHMARK = (214, 96, 42)
COLOR_GRID = (228, 228, 228)
COLOR_AXIS = (110, 110, 110)
COLOR_ZERO = (170, 170, 170)

CZECH_MONTHS = [
    "leden", "únor", "březen", "duben", "květen", "červen",
    "červenec", "srpen", "září", "říjen", "listopad", "prosinec",
]


def should_send_today(period: str, today: date) -> bool:
    if period == "daily":
        return True
    if period == "weekly":
        return today.weekday() == 0  # pondělí
    if period == "monthly":
        return today.day == 1
    return False


def _num(value) -> float:
    return float(value) if value is not None else 0.0


def _window_start(period: str, today: date) -> date:
    if period == "weekly":
        return today - timedelta(days=6)
    if period == "monthly":
        return today.replace(day=1)
    return today  # "daily" (and any unrecognized value) - just today


def _period_label(period: str, today: date) -> str:
    if period == "weekly":
        return "posledních 7 dní"
    if period == "monthly":
        return f"{CZECH_MONTHS[today.month - 1]} {today.year}"
    return f"dnešek ({today.strftime('%d.%m.%Y')})"


def _window_statistics(session: Session, portfolio_id: uuid.UUID, window_start: date, today: date) -> list[DailyStatistic]:
    return list(
        session.scalars(
            select(DailyStatistic)
            .where(
                DailyStatistic.portfolio_id == portfolio_id,
                DailyStatistic.stat_date >= window_start,
                DailyStatistic.stat_date <= today,
            )
            .order_by(DailyStatistic.stat_date)
        ).all()
    )


def _baseline_statistic(session: Session, portfolio_id: uuid.UUID, window_start: date) -> DailyStatistic | None:
    """The last DailyStatistic row strictly before the window - the "before"
    reference for a period % change even when the window itself is a single
    day (daily period), where diffing the window's own first/last row would
    always be zero."""
    return session.scalar(
        select(DailyStatistic)
        .where(DailyStatistic.portfolio_id == portfolio_id, DailyStatistic.stat_date < window_start)
        .order_by(desc(DailyStatistic.stat_date))
        .limit(1)
    )


def _top_daily_moves(stats: list[DailyStatistic], count: int = 3) -> tuple[list[DailyStatistic], list[DailyStatistic]]:
    """Only meaningful with 2+ days in the window - a single-day window (daily
    period) already has that one day's move front and center in the summary,
    so listing it again here would just duplicate the same number twice."""
    if len(stats) < 2:
        return [], []
    moved = [row for row in stats if row.daily_profit_czk is not None]
    gains = sorted(moved, key=lambda r: r.daily_profit_czk, reverse=True)[:count]
    drops = sorted(moved, key=lambda r: r.daily_profit_czk)[:count]
    return gains, drops


def _ticker_window_changes(
    session: Session, portfolio_id: uuid.UUID, range_start: date, range_end: date
) -> list[dict[str, Any]]:
    """Per-ticker price change over [range_start, range_end] for every
    currently held position - the basis for the "top/bottom stocks" rankings,
    scaled to the same window as the rest of the report (see module
    docstring). Uses live Yahoo history (same fetch_yahoo_history as the
    Grafy tab) rather than PortfolioPosition.profit_pct/profit_czk, which are
    cumulative since purchase, not scoped to this window."""
    positions = list(
        session.scalars(select(PortfolioPosition).where(PortfolioPosition.portfolio_id == portfolio_id)).all()
    )
    changes: list[dict[str, Any]] = []
    for position in positions:
        if not position.ticker or position.quantity is None:
            continue
        try:
            history = fetch_yahoo_history(position.ticker, range_start, range_end)
        except Exception:  # noqa: BLE001 - one bad ticker must not break the whole report
            logger.exception("Failed to fetch history for %s in e-mail report", position.ticker)
            continue
        points = sorted(history.get("points") or [])
        if len(points) < 2:
            continue
        first_close, last_close = float(points[0][1]), float(points[-1][1])
        if not first_close:
            continue
        rate = float(rate_for_day(session, position.currency, range_end))
        changes.append(
            {
                "ticker": position.ticker,
                "name": position.name or "",
                "pct_change": (last_close / first_close - 1) * 100,
                "czk_change": float(position.quantity) * (last_close - first_close) * rate,
            }
        )
    return changes


def _top_bottom(changes: list[dict[str, Any]], key: str, count: int = 3) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ranked = sorted(changes, key=lambda item: item[key], reverse=True)
    top = [item for item in ranked if item[key] > 0][:count]
    bottom = [item for item in reversed(ranked) if item[key] < 0][:count]
    return top, bottom


def _benchmark_pct_by_date(dates: list[date]) -> dict[date, float]:
    """S&P 500 percentage change since the first of ``dates``, keyed by date -
    same rebasing the "Zisk portfolia (%) vs S&P 500" chart in the web app
    does client-side (profitComparisonData in page.tsx), just computed here
    server-side for the e-mail chart."""
    if not dates:
        return {}
    try:
        history = fetch_yahoo_history("^GSPC", min(dates), max(dates))
    except Exception:  # noqa: BLE001 - a Yahoo outage must not break the whole report
        logger.exception("Failed to fetch S&P 500 history for e-mail report chart")
        return {}
    points = sorted(history.get("points") or [])
    if not points:
        return {}
    base = float(points[0][1])
    if not base:
        return {}
    return {point_date: (float(point_close) / base - 1) * 100 for point_date, point_close in points}


def _render_comparison_chart(stats: list[DailyStatistic]) -> bytes | None:
    if len(stats) < 2:
        return None
    dates = [row.stat_date for row in stats]
    # DailyStatistic.profit_pct is a fraction (0.05 = 5%, see recalculate_stocks) -
    # scale to percentage points so the y-axis/legend read like "1.2%", not "0.012%".
    base_profit_pct = _num(stats[0].profit_pct) * 100
    portfolio_series = [(row.stat_date, _num(row.profit_pct) * 100 - base_profit_pct) for row in stats]
    benchmark_by_date = _benchmark_pct_by_date(dates)
    benchmark_series = [(d, benchmark_by_date[d]) for d in dates if d in benchmark_by_date]
    if len(benchmark_series) < 2:
        return None

    all_values = [v for _, v in portfolio_series] + [v for _, v in benchmark_series] + [0.0]
    min_v, max_v = min(all_values), max(all_values)
    span = (max_v - min_v) or 1.0
    pad = span * 0.12
    min_v, max_v = min_v - pad, max_v + pad

    plot_w = CHART_WIDTH - CHART_MARGIN_LEFT - CHART_MARGIN_RIGHT
    plot_h = CHART_HEIGHT - CHART_MARGIN_TOP - CHART_MARGIN_BOTTOM
    date_start, date_end = dates[0], dates[-1]
    span_days = max((date_end - date_start).days, 1)

    def x_for(d: date) -> float:
        return CHART_MARGIN_LEFT + (d - date_start).days / span_days * plot_w

    def y_for(v: float) -> float:
        return CHART_MARGIN_TOP + (max_v - v) / (max_v - min_v) * plot_h

    image = Image.new("RGB", (CHART_WIDTH, CHART_HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    steps = 5
    for i in range(steps + 1):
        v = min_v + (max_v - min_v) * i / steps
        y = y_for(v)
        near_zero = min_v < 0 < max_v and abs(v) < (max_v - min_v) / (2 * steps)
        draw.line([(CHART_MARGIN_LEFT, y), (CHART_WIDTH - CHART_MARGIN_RIGHT, y)], fill=COLOR_ZERO if near_zero else COLOR_GRID)
        draw.text((4, y - 6), f"{v:.1f}%", fill=COLOR_AXIS, font=font)

    for d in (date_start, dates[len(dates) // 2], date_end):
        x = x_for(d)
        draw.text((x - 18, CHART_HEIGHT - CHART_MARGIN_BOTTOM + 8), d.strftime("%d.%m."), fill=COLOR_AXIS, font=font)

    def draw_series(series: list[tuple[date, float]], color: tuple[int, int, int]) -> None:
        points = [(x_for(d), y_for(v)) for d, v in series]
        if len(points) >= 2:
            draw.line(points, fill=color, width=3, joint="curve")

    draw_series(portfolio_series, COLOR_PORTFOLIO)
    draw_series(benchmark_series, COLOR_BENCHMARK)

    legend_y = 8
    draw.line([(CHART_MARGIN_LEFT, legend_y + 5), (CHART_MARGIN_LEFT + 20, legend_y + 5)], fill=COLOR_PORTFOLIO, width=3)
    draw.text((CHART_MARGIN_LEFT + 26, legend_y), "Portfolio", fill=COLOR_AXIS, font=font)
    draw.line([(CHART_MARGIN_LEFT + 110, legend_y + 5), (CHART_MARGIN_LEFT + 130, legend_y + 5)], fill=COLOR_BENCHMARK, width=3)
    draw.text((CHART_MARGIN_LEFT + 136, legend_y), "S&P 500", fill=COLOR_AXIS, font=font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _fmt_czk(value: float) -> str:
    return f"{value:+,.0f} Kč".replace(",", " ")


def _fmt_czk_abs(value: float) -> str:
    return f"{value:,.0f} Kč".replace(",", " ")


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f} %"


def _colored(value: float, text: str) -> str:
    color = "#1a7a3c" if value >= 0 else "#c0392b"
    return f'<span style="color:{color};font-weight:600">{text}</span>'


def _movers_table(rows: list[DailyStatistic], value_getter) -> str:
    if not rows:
        return "<p style=\"color:#777;margin:4px 0 12px\">Žádná data za toto období.</p>"
    items = "".join(
        f"<tr><td style=\"padding:2px 12px 2px 0\">{row.stat_date.strftime('%d.%m.%Y')}</td>"
        f"<td style=\"padding:2px 0\">{_colored(value_getter(row), _fmt_czk(value_getter(row)))}</td></tr>"
        for row in rows
    )
    return f"<table style=\"border-collapse:collapse;margin:4px 0 12px\">{items}</table>"


def _stock_ranking_table(rows: list[dict[str, Any]], key: str, formatter) -> str:
    if not rows:
        return "<p style=\"color:#777;margin:4px 0 12px\">Žádná data.</p>"
    items = "".join(
        f"<tr><td style=\"padding:2px 12px 2px 0\">{row['ticker']} – {row['name']}</td>"
        f"<td style=\"padding:2px 0\">{_colored(row[key], formatter(row[key]))}</td></tr>"
        for row in rows
    )
    return f"<table style=\"border-collapse:collapse;margin:4px 0 12px\">{items}</table>"


def build_report_html(session: Session, portfolio: Portfolio, today: date) -> tuple[str, bytes | None]:
    period = portfolio.report_period if portfolio.report_period in ("daily", "weekly", "monthly") else "monthly"
    window_start = _window_start(period, today)
    period_label = _period_label(period, today)

    window_stats = _window_statistics(session, portfolio.id, window_start, today)
    baseline = _baseline_statistic(session, portfolio.id, window_start)
    last = window_stats[-1] if window_stats else None

    period_profit_czk = sum(_num(row.daily_profit_czk) for row in window_stats)
    period_dividends_czk = sum(_num(row.dividends) for row in window_stats)
    period_realized_czk = sum(_num(row.realized_profit_czk) for row in window_stats)
    baseline_profit_pct = _num(baseline.profit_pct) if baseline is not None else (_num(window_stats[0].profit_pct) if window_stats else 0.0)
    period_profit_pct = ((_num(last.profit_pct) - baseline_profit_pct) * 100) if last is not None else 0.0

    chart_png = _render_comparison_chart(window_stats)

    gains, drops = _top_daily_moves(window_stats)

    # Per-ticker ranking uses the baseline day (not window_start) as its
    # comparison point for a "daily" period, whose window is just today -
    # otherwise range_start == range_end and no % change could be computed.
    ticker_range_start = baseline.stat_date if (period == "daily" and baseline is not None) else window_start
    ticker_changes = _ticker_window_changes(session, portfolio.id, ticker_range_start, today) if last is not None else []
    top_pct_gain, top_pct_loss = _top_bottom(ticker_changes, "pct_change")
    top_czk_gain, top_czk_loss = _top_bottom(ticker_changes, "czk_change")

    summary_rows = ""
    if last is not None:
        summary_rows = f"""
          <tr><td style="padding:2px 16px 2px 0;color:#555">Hodnota portfolia</td><td>{_fmt_czk_abs(_num(last.total_value_czk))}</td></tr>
          <tr><td style="padding:2px 16px 2px 0;color:#555">Nerealizovaný zisk celkem</td><td>{_colored(_num(last.unrealized_profit_czk), _fmt_czk(_num(last.unrealized_profit_czk)))}</td></tr>
          <tr><td style="padding:2px 16px 2px 0;color:#555">Zisk za {period_label}</td><td>{_colored(period_profit_czk, _fmt_czk(period_profit_czk))}</td></tr>
          <tr><td style="padding:2px 16px 2px 0;color:#555">Zisk % za {period_label}</td><td>{_colored(period_profit_pct, _fmt_pct(period_profit_pct))}</td></tr>
          <tr><td style="padding:2px 16px 2px 0;color:#555">Dividendy za {period_label}</td><td>{_fmt_czk_abs(period_dividends_czk)}</td></tr>
          <tr><td style="padding:2px 16px 2px 0;color:#555">Realizovaný zisk za {period_label}</td><td>{_colored(period_realized_czk, _fmt_czk(period_realized_czk))}</td></tr>
        """
    else:
        summary_rows = '<tr><td style="color:#777">Zatím nejsou k dispozici žádná data.</td></tr>'

    chart_html = ""
    if chart_png:
        chart_html = '<h3 style="margin:24px 0 8px">Zisk portfolia (%) vs S&P 500</h3><img src="cid:comparison_chart" width="860" height="380" alt="Graf porovnání s S&P 500" />'

    movers_html = ""
    if gains or drops:
        movers_html = f"""
          <h3 style="margin:24px 0 8px">Tři největší denní nárůsty ({period_label})</h3>
          {_movers_table(gains, lambda r: _num(r.daily_profit_czk))}

          <h3 style="margin:0 0 8px">Tři největší denní poklesy ({period_label})</h3>
          {_movers_table(drops, lambda r: _num(r.daily_profit_czk))}
        """

    html = f"""
    <html>
    <body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:900px">
      <h2 style="margin-bottom:0">{portfolio.name}</h2>
      <p style="color:#555;margin-top:4px">Statistika za {period_label} (k {today.strftime('%d.%m.%Y')})</p>

      <h3 style="margin:20px 0 8px">Statistika za {period_label}</h3>
      <table style="border-collapse:collapse">{summary_rows}</table>

      {chart_html}

      {movers_html}

      <h3 style="margin:24px 0 8px">Nejvýnosnější akcie (%) – {period_label}</h3>
      {_stock_ranking_table(top_pct_gain, "pct_change", _fmt_pct)}

      <h3 style="margin:0 0 8px">Nejztrátovější akcie (%) – {period_label}</h3>
      {_stock_ranking_table(top_pct_loss, "pct_change", _fmt_pct)}

      <h3 style="margin:24px 0 8px">Nejvýnosnější akcie (Kč) – {period_label}</h3>
      {_stock_ranking_table(top_czk_gain, "czk_change", _fmt_czk)}

      <h3 style="margin:0 0 8px">Nejztrátovější akcie (Kč) – {period_label}</h3>
      {_stock_ranking_table(top_czk_loss, "czk_change", _fmt_czk)}

      <p style="color:#999;font-size:12px;margin-top:28px">Automatický report z FinanceSEMA - nastavení příjemce a periody v záložce Subjekty.</p>
    </body>
    </html>
    """
    return html, chart_png


def resolve_smtp_config(session: Session) -> dict[str, Any]:
    """SMTP credentials, preferring the single-row `smtp_settings` DB table
    (editable from the app's own Nastavení tab) over the SMTP_* env vars -
    falls back field-by-field to the env vars for anything left blank in the
    DB row (or if there's no row at all yet), so an existing env-based setup
    keeps working until someone fills in the UI."""
    env = get_settings()
    row = session.get(SmtpSettings, "default")
    return {
        "host": (row.host if row and row.host else env.smtp_host),
        "port": (row.port if row and row.port else env.smtp_port),
        "username": (row.username if row and row.username else env.smtp_username),
        "password": (row.password if row and row.password else env.smtp_password),
        "from_address": (row.from_address if row and row.from_address else env.smtp_from),
        "use_tls": (row.use_tls if row is not None else env.smtp_use_tls),
    }


def _smtp_send(config: dict[str, Any], to_addrs: list[str], message: MIMEMultipart) -> None:
    if not config["host"] or not config["from_address"]:
        raise RuntimeError("SMTP není nakonfigurováno - vyplňte ho v záložce Nastavení (nebo SMTP_HOST/SMTP_FROM v .env)")
    with smtplib.SMTP(config["host"], config["port"] or 587, timeout=30) as smtp:
        if config["use_tls"]:
            smtp.starttls()
        if config["username"] and config["password"]:
            smtp.login(config["username"], config["password"])
        smtp.sendmail(config["from_address"], to_addrs, message.as_string())


def _send_email(config: dict[str, Any], to_addrs: list[str], subject: str, html_body: str, chart_png: bytes | None) -> None:
    message = MIMEMultipart("related")
    message["Subject"] = subject
    message["From"] = config["from_address"] or ""
    message["To"] = ", ".join(to_addrs)

    alternative = MIMEMultipart("alternative")
    message.attach(alternative)
    alternative.attach(MIMEText(html_body, "html", "utf-8"))

    if chart_png:
        image = MIMEImage(chart_png)
        image.add_header("Content-ID", "<comparison_chart>")
        image.add_header("Content-Disposition", "inline", filename="graf.png")
        message.attach(image)

    _smtp_send(config, to_addrs, message)


def send_portfolio_report(session: Session, portfolio: Portfolio, today: date | None = None) -> None:
    today = today or date.today()
    recipients = [addr.strip() for addr in (portfolio.report_email or "").split(",") if addr.strip()]
    if not recipients:
        return
    html_body, chart_png = build_report_html(session, portfolio, today)
    subject = f"FinanceSEMA – {portfolio.name}: statistika ({today.strftime('%d.%m.%Y')})"
    config = resolve_smtp_config(session)
    _send_email(config, recipients, subject, html_body, chart_png)


def send_test_email(session: Session, to_address: str) -> None:
    config = resolve_smtp_config(session)
    message = MIMEMultipart()
    message["Subject"] = "FinanceSEMA – testovací e-mail"
    message["From"] = config["from_address"] or ""
    message["To"] = to_address
    message.attach(MIMEText("Toto je testovací e-mail z FinanceSEMA - SMTP nastavení funguje.", "plain", "utf-8"))
    _smtp_send(config, [to_address], message)
