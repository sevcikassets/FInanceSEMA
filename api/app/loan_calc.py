"""Pure loan/mortgage interest projection helpers.

The `Investice` sheet of `Finance SEMA.xlsm` contains a partially built
projection of annual mortgage interest (columns 2026-2055) using this
Excel formula (LET + CUMIPMT), copied into only a handful of cells and
never filled in for the real mortgage rows:

    =LET(pv, H<row>, rate, L<row>, sd, J<row>, ed, K<row>, yrs, M<row>,
         nper, IF(yrs<>"", yrs*12, IF(ed<>"", DATEDIF(sd,ed,"m")+1, "")),
         y, <year>,
         sy, YEAR(sd), sm, MONTH(sd),
         sp, MAX(1, (y-sy)*12 - sm + 2),
         ep, MIN(nper, (y-sy)*12 + 13 - sm),
         IF(sp>ep, "", -CUMIPMT(rate/12, nper, pv, sp, ep, 0)))

`pv` = borrowed_amount, `rate` = annual interest_rate, `sd` = borrowed_from,
`ed` = borrowed_to, `yrs` = loan_years.

This module re-implements the same computation in plain Python (a standard
fixed-payment / ordinary-annuity amortization, matching Excel's CUMIPMT), so
the application can compute the projection for any asset directly - instead
of depending on hand-filled spreadsheet cells that, for the real mortgage in
the source workbook, were never actually populated.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, TypedDict

TWO_PLACES = Decimal("0.01")

DEFAULT_PROJECTION_YEARS = range(2026, 2056)


def monthly_rate(annual_rate: float) -> float:
    return annual_rate / 12


def number_of_periods(
    loan_years: float | Decimal | None,
    borrowed_from: date | None,
    borrowed_to: date | None,
) -> int | None:
    """Total number of monthly payments (Excel `nper`)."""
    if loan_years:
        return round(float(loan_years) * 12)
    if borrowed_from and borrowed_to:
        months = (borrowed_to.year - borrowed_from.year) * 12 + (borrowed_to.month - borrowed_from.month)
        return months + 1
    return None


def period_window(year: int, start_year: int, start_month: int, nper: int) -> tuple[int, int]:
    """Excel: sp = MAX(1, (y-sy)*12 - sm + 2), ep = MIN(nper, (y-sy)*12 + 13 - sm)."""
    start_period = max(1, (year - start_year) * 12 - start_month + 2)
    end_period = min(nper, (year - start_year) * 12 + 13 - start_month)
    return start_period, end_period


def cumulative_interest(pv: float, rate: float, nper: int, start_period: int, end_period: int) -> float:
    """Sum of interest paid between start_period and end_period (inclusive, 1-indexed
    monthly payments) for a standard fixed-payment ordinary annuity (payments at the
    end of each period). Equivalent to Excel's CUMIPMT(rate, nper, pv, sp, ep, 0).
    """
    if start_period > end_period or nper <= 0 or pv <= 0:
        return 0.0
    if rate == 0:
        payment = pv / nper
    else:
        payment = pv * rate / (1 - (1 + rate) ** -nper)
    balance = pv
    total_interest = 0.0
    for period in range(1, end_period + 1):
        interest = balance * rate if rate else 0.0
        principal = payment - interest
        balance -= principal
        if period >= start_period:
            total_interest += interest
    return total_interest


def project_annual_interest(
    borrowed_amount: Decimal | float | None,
    interest_rate: Decimal | float | None,
    borrowed_from: date | None,
    loan_years: Decimal | float | None = None,
    borrowed_to: date | None = None,
    years: Iterable[int] = DEFAULT_PROJECTION_YEARS,
) -> dict[int, Decimal]:
    """Project interest paid per calendar year for a fixed-payment loan.

    Returns ``{year: interest_czk}``. Years with no scheduled payments are omitted,
    matching how the original spreadsheet formula returns "" outside the loan term.
    Missing inputs (no loan on the asset) return an empty dict.
    """
    if not borrowed_amount or not interest_rate or not borrowed_from:
        return {}
    nper = number_of_periods(loan_years, borrowed_from, borrowed_to)
    if not nper or nper <= 0:
        return {}

    pv = float(borrowed_amount)
    rate = monthly_rate(float(interest_rate))
    start_year = borrowed_from.year
    start_month = borrowed_from.month

    result: dict[int, Decimal] = {}
    for year in years:
        start_period, end_period = period_window(year, start_year, start_month, nper)
        if start_period > end_period:
            continue
        interest = cumulative_interest(pv, rate, nper, start_period, end_period)
        result[year] = Decimal(str(round(interest, 2))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return result


class SchedulePeriod(TypedDict):
    period: int
    date: date
    payment: Decimal
    principal: Decimal
    interest: Decimal
    balance: Decimal


def add_months(start: date, months: int) -> date:
    """Add whole calendar months to a date, clamping the day to the target
    month's length (e.g. Jan 31 + 1 month -> Feb 28/29)."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def amortization_schedule(
    pv: Decimal | float | None,
    interest_rate: Decimal | float | None,
    start_date: date | None,
    loan_years: Decimal | float | None = None,
    end_date: date | None = None,
    first_payment_date: date | None = None,
    first_payment_amount: Decimal | float | None = None,
) -> list[SchedulePeriod]:
    """Full monthly payment schedule for a fixed-payment loan: one row per
    payment period with the payment split into principal/interest and the
    remaining balance after that payment.

    Same amortization model as project_annual_interest/cumulative_interest
    (a standard fixed-payment ordinary annuity), just returned period-by-
    period instead of summed per calendar year. Missing inputs return an
    empty list, matching project_annual_interest's contract.

    first_payment_date/first_payment_amount override the very first payment
    when it differs from the regular ones - a common real mortgage shape,
    where a prorated interest-only payment covers the gap between drawdown
    and the first regular installment date. When both are given, that first
    payment counts entirely as interest (it doesn't reduce the principal),
    and the remaining nper-1 payments are re-amortized over the still-full
    principal, dated from first_payment_date onward rather than start_date,
    since that's when the regular monthly cadence actually begins. Omitting
    either leaves the schedule exactly as before (a single uniform annuity).
    """
    if not pv or not interest_rate or not start_date:
        return []
    nper = number_of_periods(loan_years, start_date, end_date)
    if not nper or nper <= 0:
        return []

    principal_value = float(pv)
    rate = monthly_rate(float(interest_rate))

    schedule: list[SchedulePeriod] = []
    balance = principal_value
    remaining_periods = nper
    anchor_date = start_date
    period_offset = 0

    if first_payment_date and first_payment_amount:
        first_payment = float(first_payment_amount)
        schedule.append(
            {
                "period": 1,
                "date": first_payment_date,
                "payment": Decimal(str(round(first_payment, 2))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
                "principal": Decimal("0.00"),
                "interest": Decimal(str(round(first_payment, 2))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
                "balance": Decimal(str(round(balance, 2))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
            }
        )
        remaining_periods = nper - 1
        anchor_date = first_payment_date
        period_offset = 1
        if remaining_periods <= 0:
            return schedule

    payment = (
        principal_value / remaining_periods
        if rate == 0
        else principal_value * rate / (1 - (1 + rate) ** -remaining_periods)
    )
    for offset in range(1, remaining_periods + 1):
        interest = balance * rate if rate else 0.0
        principal = payment - interest
        balance -= principal
        schedule.append(
            {
                "period": period_offset + offset,
                "date": add_months(anchor_date, offset),
                "payment": Decimal(str(round(payment, 2))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
                "principal": Decimal(str(round(principal, 2))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
                "interest": Decimal(str(round(interest, 2))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
                "balance": Decimal(str(round(max(balance, 0.0), 2))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
            }
        )
    return schedule


def annual_interest_from_schedule(
    schedule: Iterable[SchedulePeriod], years: Iterable[int] = DEFAULT_PROJECTION_YEARS
) -> dict[int, Decimal]:
    """Sums a monthly amortization_schedule()'s interest into calendar years.

    Used instead of project_annual_interest's closed-form CUMIPMT-style calc
    whenever a first-payment override makes the schedule non-uniform - that
    closed form assumes every period lands exactly one calendar month after
    the last (a pure function of loan_years/borrowed_from), which no longer
    holds once the first payment has its own independent real date.
    """
    allowed_years = set(years)
    totals: dict[int, Decimal] = {}
    for period in schedule:
        year = period["date"].year
        if year not in allowed_years:
            continue
        totals[year] = totals.get(year, Decimal("0")) + period["interest"]
    return {year: value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP) for year, value in totals.items()}
