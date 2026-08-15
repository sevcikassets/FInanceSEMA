"""Unit tests for app.loan_calc. Pure functions, no database required."""

from datetime import date
from decimal import Decimal

from app.loan_calc import (
    add_months,
    amortization_schedule,
    cumulative_interest,
    monthly_rate,
    number_of_periods,
    period_window,
    project_annual_interest,
)


def test_number_of_periods_from_loan_years():
    assert number_of_periods(15, None, None) == 180


def test_number_of_periods_from_date_range():
    assert number_of_periods(None, date(2024, 1, 1), date(2024, 12, 1)) == 12


def test_number_of_periods_missing_inputs():
    assert number_of_periods(None, None, None) is None
    assert number_of_periods(None, date(2024, 1, 1), None) is None


def test_period_window_first_year_partial():
    # Loan starting July 2024: 2024 only covers periods 1-6 (Jul-Dec).
    sp, ep = period_window(2024, 2024, 7, nper=180)
    assert (sp, ep) == (1, 6)


def test_period_window_full_year():
    sp, ep = period_window(2025, 2024, 7, nper=180)
    assert (sp, ep) == (7, 18)


def test_period_window_after_loan_ends():
    sp, ep = period_window(2040, 2024, 7, nper=180)
    # nper=180 (15y) starting mid-2024 ends in 2039; 2040 has no periods left.
    assert sp > ep


def test_cumulative_interest_matches_total_amortization():
    pv = 1_000_000.0
    rate = monthly_rate(0.05)
    nper = 180
    payment = pv * rate / (1 - (1 + rate) ** -nper)
    expected_total_interest = payment * nper - pv
    actual = cumulative_interest(pv, rate, nper, 1, nper)
    assert abs(actual - expected_total_interest) < 0.01


def test_cumulative_interest_zero_rate_is_flat():
    # With 0% interest, no interest accrues at all.
    assert cumulative_interest(120_000.0, 0.0, 12, 1, 12) == 0.0


def test_cumulative_interest_invalid_window_returns_zero():
    assert cumulative_interest(100_000.0, 0.01, 12, 8, 3) == 0.0


def test_project_annual_interest_empty_without_loan_params():
    assert project_annual_interest(None, None, None) == {}
    assert project_annual_interest(Decimal("100000"), None, date(2024, 1, 1)) == {}


def test_project_annual_interest_sums_to_full_term_interest():
    pv = 4_000_000.0
    annual_rate = 0.0489
    borrowed_from = date(2024, 7, 1)
    loan_years = 15
    nper = number_of_periods(loan_years, borrowed_from, None)
    rate = monthly_rate(annual_rate)
    expected_total = cumulative_interest(pv, rate, nper, 1, nper)

    projection = project_annual_interest(
        borrowed_amount=pv,
        interest_rate=annual_rate,
        borrowed_from=borrowed_from,
        loan_years=loan_years,
        years=range(2024, 2045),
    )
    # Per-year values are individually rounded to whole cents, so the sum may be
    # off from the exact full-term total by at most a few cents.
    assert abs(sum(projection.values()) - Decimal(str(round(expected_total, 2)))) < Decimal("1.00")
    # 2026 (a full calendar year within the loan term) must be present and positive.
    assert projection[2026] > 0
    # Years past the loan term must not appear.
    assert 2044 not in projection


def test_add_months_clamps_day_to_target_month_length():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # 2024 is a leap year
    assert add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert add_months(date(2024, 1, 15), 12) == date(2025, 1, 15)
    assert add_months(date(2024, 10, 1), 3) == date(2025, 1, 1)


def test_amortization_schedule_empty_without_loan_params():
    assert amortization_schedule(None, None, None) == []
    assert amortization_schedule(Decimal("100000"), None, date(2024, 1, 1)) == []


def test_amortization_schedule_matches_real_mortgage_shape():
    # Same figures as the real "004/26-HYP" Hypotéka row this feature was
    # built around - a good end-to-end sanity check for the whole formula,
    # not just an isolated edge case.
    schedule = amortization_schedule(
        pv=Decimal("4506000"),
        interest_rate=Decimal("0.0489"),
        start_date=date(2026, 7, 1),
        loan_years=Decimal("15"),
    )
    assert len(schedule) == 180  # 15 years * 12
    assert schedule[0]["date"] == date(2026, 8, 1)
    assert schedule[-1]["balance"] == Decimal("0.00")
    # Every payment should be identical (fixed-payment annuity).
    assert len({row["payment"] for row in schedule}) == 1
    # Sum of principal across every period must equal the original principal
    # (within a few cents of rounding drift across 180 periods).
    total_principal = sum(row["principal"] for row in schedule)
    assert abs(total_principal - Decimal("4506000")) < Decimal("1.00")
    # Interest paid should decrease and principal paid should increase over
    # time, as the outstanding balance amortizes down.
    assert schedule[0]["interest"] > schedule[-1]["interest"]
    assert schedule[0]["principal"] < schedule[-1]["principal"]


def test_amortization_schedule_zero_interest_rate_returns_empty():
    # Same "falsy input -> []" contract as project_annual_interest - a
    # Decimal("0") interest_rate is falsy, so this short-circuits before any
    # computation (matching the existing project_annual_interest guard this
    # function was deliberately written to mirror), not a flat 0%-interest
    # schedule.
    assert amortization_schedule(pv=Decimal("120000"), interest_rate=Decimal("0"), start_date=date(2024, 1, 1), loan_years=1) == []
