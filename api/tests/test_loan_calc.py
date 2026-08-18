"""Unit tests for app.loan_calc. Pure functions, no database required."""

from datetime import date
from decimal import Decimal

import pytest

from app.loan_calc import (
    add_months,
    amortization_schedule,
    annual_interest_from_schedule,
    cumulative_interest,
    loan_movement_annual_interest,
    loan_movement_schedule,
    loan_relationship_interest_in_range,
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


def test_amortization_schedule_without_first_payment_override_is_unchanged():
    # Omitting first_payment_date/first_payment_amount must produce exactly
    # the same schedule as before the override existed - no behaviour change
    # for the (overwhelmingly common) case where no override is set.
    kwargs = dict(pv=Decimal("4506000"), interest_rate=Decimal("0.0489"), start_date=date(2026, 7, 1), loan_years=Decimal("15"))
    assert amortization_schedule(**kwargs) == amortization_schedule(**kwargs, first_payment_date=None, first_payment_amount=None)


def test_amortization_schedule_first_payment_override_is_pure_interest():
    pv = Decimal("4506000")
    schedule = amortization_schedule(
        pv=pv,
        interest_rate=Decimal("0.0489"),
        start_date=date(2026, 7, 1),
        loan_years=Decimal("15"),
        first_payment_date=date(2026, 7, 20),
        first_payment_amount=Decimal("12345.67"),
    )
    assert len(schedule) == 180  # still 15 years * 12 total payments
    first = schedule[0]
    assert first["period"] == 1
    assert first["date"] == date(2026, 7, 20)
    assert first["payment"] == Decimal("12345.67")
    assert first["interest"] == Decimal("12345.67")
    assert first["principal"] == Decimal("0.00")
    # The principal wasn't touched by the first payment, so the balance right
    # after it is still the full original amount.
    assert first["balance"] == pv

    # Periods 2.. are dated from first_payment_date onward, not start_date.
    assert schedule[1]["date"] == date(2026, 8, 20)
    assert schedule[1]["period"] == 2

    # The regular payment (periods 2..180) re-amortizes the full, untouched
    # principal over the 179 remaining periods, so it differs from what the
    # uniform 180-period schedule would have used.
    plain_schedule = amortization_schedule(
        pv=pv, interest_rate=Decimal("0.0489"), start_date=date(2026, 7, 1), loan_years=Decimal("15")
    )
    assert schedule[1]["payment"] != plain_schedule[1]["payment"]
    # Every regular payment (excluding the overridden first one) is identical.
    assert len({row["payment"] for row in schedule[1:]}) == 1
    # Total principal repaid across periods 2.. must still equal the full pv.
    total_principal = sum(row["principal"] for row in schedule[1:])
    assert abs(total_principal - pv) < Decimal("1.00")
    assert schedule[-1]["balance"] == Decimal("0.00")


def test_amortization_schedule_first_payment_override_ignored_when_partial():
    # Both fields must be given together - a date with no amount (or vice
    # versa) falls back to the regular uniform schedule rather than half-
    # applying the override.
    kwargs = dict(pv=Decimal("120000"), interest_rate=Decimal("0.05"), start_date=date(2024, 1, 1), loan_years=1)
    assert amortization_schedule(**kwargs, first_payment_date=date(2024, 1, 15)) == amortization_schedule(**kwargs)
    assert amortization_schedule(**kwargs, first_payment_amount=Decimal("500")) == amortization_schedule(**kwargs)


def test_annual_interest_from_schedule_sums_by_calendar_year():
    schedule = amortization_schedule(
        pv=Decimal("4506000"),
        interest_rate=Decimal("0.0489"),
        start_date=date(2026, 7, 1),
        loan_years=Decimal("15"),
        first_payment_date=date(2026, 7, 20),
        first_payment_amount=Decimal("18000"),
    )
    totals = annual_interest_from_schedule(schedule, years=range(2026, 2028))
    expected_2026 = sum(
        (row["interest"] for row in schedule if row["date"].year == 2026), Decimal("0")
    )
    assert totals[2026] == expected_2026
    assert 2028 not in totals


# A LoanMovement (Zápůjčka/Půjčka) is not a mortgage - it has no schedule of
# its own principal repayments, so its interest must be plain simple interest
# on a constant (or, when several movements share a lender/borrower pair,
# running) balance (see loan_relationship_interest_in_range's docstring),
# NOT amortization_schedule's fixed-payment annuity model (which would make
# the "interest" portion shrink every month as if principal were being paid
# down, even though it isn't).


def test_loan_relationship_interest_missing_rate_or_empty_returns_zero():
    assert loan_relationship_interest_in_range([], date(2024, 1, 1), date(2024, 1, 31)) == Decimal("0")
    assert loan_relationship_interest_in_range(
        [(date(2024, 1, 1), Decimal("100000"), None)], date(2024, 1, 1), date(2024, 1, 31)
    ) == Decimal("0")


def test_loan_relationship_interest_is_simple_interest_on_full_days():
    # 1 200 000 * 6% * 31/365 (January) - plain principal * rate * days/365,
    # not a monthly_rate/12 approximation. A single movement is the simplest
    # possible "relationship" (just itself).
    interest = loan_relationship_interest_in_range(
        [(date(2024, 1, 1), Decimal("1200000"), Decimal("0.06"))], date(2024, 1, 1), date(2024, 1, 31)
    )
    expected = (Decimal("1200000") * Decimal("0.06") * Decimal(31) / Decimal(365)).quantize(Decimal("0.01"))
    assert interest == expected


def test_loan_relationship_interest_zero_before_movement_date():
    assert loan_relationship_interest_in_range(
        [(date(2024, 8, 1), Decimal("1200000"), Decimal("0.06"))], date(2024, 1, 1), date(2024, 7, 31)
    ) == Decimal("0")


def test_loan_relationship_interest_multiple_movements_nets_running_balance():
    # Real data pattern this feature was built for: several draws/repayments
    # between the SAME lender/borrower pair over time (not one row per
    # loan), netting to exactly 0 once fully repaid. Only the first movement
    # carries an explicit rate - it must carry forward to the later,
    # un-rated draws/repayments (a rate is entered once, not on every row).
    movements = [
        (date(2024, 5, 14), Decimal("250000"), Decimal("0.04")),
        (date(2024, 5, 29), Decimal("500000"), Decimal("0.04")),
        (date(2024, 7, 12), Decimal("-500000"), None),
        (date(2024, 9, 26), Decimal("750000"), None),
        (date(2025, 2, 6), Decimal("-1000000"), None),
    ]
    # Balance after each event: 250k, 750k, 250k, 1 000k, 0.
    assert sum(amount for _, amount, _ in movements) == Decimal("0")

    # June: constant 750 000 balance for the whole month at the carried-
    # forward 4% rate.
    june_interest = loan_relationship_interest_in_range(movements, date(2024, 6, 1), date(2024, 6, 30))
    expected_june = (Decimal("750000") * Decimal("0.04") * Decimal(30) / Decimal(365)).quantize(Decimal("0.01"))
    assert june_interest == expected_june

    # After full repayment (balance 0), no further interest accrues even
    # though the relationship technically still exists.
    assert loan_relationship_interest_in_range(movements, date(2025, 3, 1), date(2025, 3, 31)) == Decimal("0")


def test_loan_relationship_interest_stops_at_completed_at_reversal():
    # The caller (classify_loan_interest) folds a movement's own completed_at
    # in as a same-relationship reversal event dated the day after, so
    # interest still accrues through completed_at inclusive.
    events = [
        (date(2024, 7, 1), Decimal("1200000"), Decimal("0.06")),
        (date(2024, 7, 16), Decimal("-1200000"), None),  # completed_at (7/15) + 1 day
    ]
    interest = loan_relationship_interest_in_range(events, date(2024, 7, 1), date(2024, 7, 31))
    expected = (Decimal("1200000") * Decimal("0.06") * Decimal(15) / Decimal(365)).quantize(Decimal("0.01"))
    assert interest == expected


def test_loan_movement_annual_interest_requires_planned_end_date():
    assert loan_movement_annual_interest(Decimal("1200000"), Decimal("0.06"), date(2024, 1, 1), None) == {}


def test_loan_movement_annual_interest_sums_full_year_close_to_rate_times_principal():
    # A loan outstanding for a full calendar year should accrue very close
    # to principal * rate (not principal * rate * some fraction, and
    # nowhere near the front-loaded total a fixed-payment amortization
    # would produce). 2024 is a leap year (366 days) under a fixed ACT/365
    # convention, so the exact total is slightly above principal * rate -
    # a ~1% tolerance comfortably covers that, not a bug.
    totals = loan_movement_annual_interest(
        Decimal("1200000"), Decimal("0.06"), date(2024, 1, 1), date(2024, 12, 31), years=range(2024, 2025)
    )
    assert float(totals[2024]) == pytest.approx(1200000 * 0.06, rel=0.01)


def test_loan_movement_schedule_empty_without_end_date():
    assert loan_movement_schedule(Decimal("1200000"), Decimal("0.06"), date(2024, 1, 1), None) == []


def test_loan_movement_schedule_flat_balance_until_bullet_repayment():
    schedule = loan_movement_schedule(Decimal("1200000"), Decimal("0.06"), date(2024, 1, 1), date(2024, 12, 31))
    assert len(schedule) == 12
    # Unlike a mortgage, principal isn't paid down monthly - balance stays
    # flat at the full amount through every row except the last.
    for row in schedule[:-1]:
        assert row["principal"] == Decimal("0.00")
        assert row["balance"] == Decimal("1200000.00")
    assert schedule[-1]["principal"] == Decimal("1200000.00")
    assert schedule[-1]["balance"] == Decimal("0.00")
    assert schedule[-1]["date"] == date(2024, 12, 31)

    # The core regression this feature fixes: interest must stay in a tight
    # band (varying only with each month's day count), NOT decay the way a
    # fixed-payment amortization schedule's interest portion would.
    interests = [row["interest"] for row in schedule]
    assert max(interests) / min(interests) < Decimal("1.15")  # 31 vs 28 days is an ~11% spread


def test_loan_movement_schedule_stops_at_completed_at_mid_month():
    schedule = loan_movement_schedule(
        Decimal("1200000"), Decimal("0.06"), date(2024, 1, 1), date(2024, 12, 31), completed_at=date(2024, 7, 15)
    )
    assert len(schedule) == 7  # Jan..Jul
    assert schedule[-1]["date"] == date(2024, 7, 15)
    assert schedule[-1]["principal"] == Decimal("1200000.00")
    assert schedule[-1]["balance"] == Decimal("0.00")
    expected_july_interest = (Decimal("1200000") * Decimal("0.06") * Decimal(15) / Decimal(365)).quantize(Decimal("0.01"))
    assert schedule[-1]["interest"] == expected_july_interest
