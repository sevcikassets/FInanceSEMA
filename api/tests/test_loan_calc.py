"""Unit tests for app.loan_calc. Pure functions, no database required."""

from datetime import date
from decimal import Decimal

from app.loan_calc import (
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
