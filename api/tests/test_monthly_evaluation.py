"""Tests for the Vyhodnocení (monthly evaluation) feature: exact realized-
gain tracking in recalculate_stocks, PortfolioSelfParty-based interest
classification, and the stored/upserted MonthlyEvaluation report.

All figures below are fictional test fixtures, not real financial data.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from .conftest import requires_db


def _approx_ratio(base: float, ratio: float, tolerance: float = 0.05) -> object:
    return pytest.approx(base * ratio, rel=tolerance)


@requires_db
def test_recalculate_stocks_tracks_exact_realized_profit_and_weekend_sell(db_session, portfolio_id, monkeypatch):
    """Realized gain = sale proceeds - average-cost basis removed, computed
    exactly (no price-history lookups needed) at the moment of each sell.
    The sell lands on a weekend with no other same-day activity - before the
    calendar_dates fix, no DailyStatistic row would exist for that day at
    all, silently dropping its realized profit."""
    from app import stock_services
    from app.models import DailyStatistic, StockTransaction

    monkeypatch.setattr(stock_services, "fetch_yahoo_history", lambda ticker, date_from, date_to: {"currency": "CZK", "points": []})

    monday_buy = date(2024, 1, 8)
    saturday_sell = date(2024, 1, 13)

    db_session.add(
        StockTransaction(
            portfolio_id=portfolio_id,
            traded_on=monday_buy,
            movement_type="Nákup",
            instrument_name="Test Corp",
            ticker="TEST",
            quantity=Decimal("10"),
            unit_price_ccy=Decimal("100"),
            gross_amount_ccy=Decimal("1000"),
            currency="CZK",
            amount_czk=Decimal("1000"),
        )
    )
    db_session.add(
        StockTransaction(
            portfolio_id=portfolio_id,
            traded_on=saturday_sell,
            movement_type="Prodej",
            instrument_name="Test Corp",
            ticker="TEST",
            quantity=Decimal("-4"),
            currency="CZK",
            amount_czk=Decimal("600"),  # sale proceeds
        )
    )
    db_session.commit()

    stock_services.recalculate_stocks(db_session, portfolio_id, dry_run=False)

    sell_row = db_session.get(DailyStatistic, (portfolio_id, saturday_sell))
    assert sell_row is not None  # weekend-sell regression: row must exist at all
    # average_cost = 1000/10 = 100; cost_basis_removed = 4*100 = 400; realized = 600-400 = 200
    assert sell_row.realized_profit_czk == Decimal("200.00")
    assert sell_row.realized_profit_total_czk == Decimal("200.00")


@requires_db
def test_evaluation_classifies_interest_by_self_party_membership(client, db_session, portfolio_id):
    """A loan counts toward interest received/paid only when exactly one
    side is a self-party for this Subjekt - self<->self (internal transfer)
    and external<->external (not this Subjekt's business) are excluded."""
    from app.models import LoanMovement, Party

    self_a = Party(name="Self A", kind="unknown")
    self_d = Party(name="Self D", kind="unknown")
    external_b = Party(name="External B", kind="unknown")
    external_c = Party(name="External C", kind="unknown")
    db_session.add_all([self_a, self_d, external_b, external_c])
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    set_self = client.put(
        f"/portfolios/{portfolio_id}/self-parties",
        headers=headers,
        json={"party_ids": [str(self_a.id), str(self_d.id)]},
    )
    assert set_self.status_code == 200
    assert {row["name"] for row in set_self.json()} == {"Self A", "Self D"}

    loan_terms = dict(movement_date=date(2024, 1, 1), planned_end_date=date(2024, 12, 31), interest_rate=Decimal("0.12"))
    db_session.add_all(
        [
            LoanMovement(portfolio_id=portfolio_id, lender_id=self_a.id, borrower_id=external_b.id, amount=Decimal("120000"), **loan_terms),
            LoanMovement(portfolio_id=portfolio_id, lender_id=external_c.id, borrower_id=self_a.id, amount=Decimal("60000"), **loan_terms),
            LoanMovement(portfolio_id=portfolio_id, lender_id=self_a.id, borrower_id=self_d.id, amount=Decimal("999999"), **loan_terms),
            LoanMovement(portfolio_id=portfolio_id, lender_id=external_b.id, borrower_id=external_c.id, amount=Decimal("999999"), **loan_terms),
        ]
    )
    db_session.commit()

    computed = client.post("/evaluations/compute", headers=headers, params={**params, "period": "2024-02"})
    assert computed.status_code == 200
    body = computed.json()
    assert body["interest_received_czk"] > 0  # from Self A -> External B only
    assert body["interest_paid_czk"] > 0  # from External C -> Self A only
    # 120000 principal earns roughly double the interest of the 60000 one at
    # the same rate/term - confirms the self<->self and external<->external
    # loans (999999 principal each) were excluded, not just netted small.
    assert body["interest_received_czk"] == _approx_ratio(body["interest_paid_czk"], 2)


@requires_db
def test_evaluation_compute_upserts_not_accumulates(client, db_session, portfolio_id):
    from app.models import Asset, AssetCost, MonthlyEvaluation

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id), "period": "2024-03"}

    asset = Asset(portfolio_id=portfolio_id, code="A-1", name="Test Asset")
    db_session.add(asset)
    db_session.commit()
    db_session.add(AssetCost(portfolio_id=portfolio_id, asset_id=asset.id, item="První", amount=Decimal("100"), cost_date=date(2024, 3, 5)))
    db_session.commit()

    first = client.post("/evaluations/compute", headers=headers, params=params)
    assert first.status_code == 200
    assert first.json()["asset_cashflows"][0]["expense_czk"] == 100

    db_session.add(AssetCost(portfolio_id=portfolio_id, asset_id=asset.id, item="Druhý", amount=Decimal("50"), cost_date=date(2024, 3, 10)))
    db_session.commit()

    second = client.post("/evaluations/compute", headers=headers, params=params)
    assert second.status_code == 200
    assert second.json()["asset_cashflows"][0]["expense_czk"] == 150  # both costs summed, not accumulated across calls

    stored = db_session.query(MonthlyEvaluation).filter_by(portfolio_id=portfolio_id, period="2024-03").all()
    assert len(stored) == 1  # upserted, not duplicated
    assert first.json()["id"] == second.json()["id"]


@requires_db
def test_evaluation_asset_cashflow_splits_income_and_expense(client, db_session, portfolio_id):
    """AssetCost.amount is already signed (positive=expense, negative=income,
    e.g. a scrap-metal sale) - the per-asset breakdown must split them into
    separate income_czk/expense_czk figures, not just net them."""
    from app.models import Asset, AssetCost

    asset = Asset(portfolio_id=portfolio_id, code="A-2", name="Split Asset")
    db_session.add(asset)
    db_session.commit()
    db_session.add_all(
        [
            AssetCost(portfolio_id=portfolio_id, asset_id=asset.id, item="Výdaj", amount=Decimal("500"), cost_date=date(2024, 4, 1)),
            AssetCost(portfolio_id=portfolio_id, asset_id=asset.id, item="Výkup šrotu", amount=Decimal("-120"), cost_date=date(2024, 4, 15)),
            AssetCost(portfolio_id=portfolio_id, asset_id=None, item="Bez majetku", amount=Decimal("30"), cost_date=date(2024, 4, 20)),
        ]
    )
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    response = client.post("/evaluations/compute", headers=headers, params={"portfolio_id": str(portfolio_id), "period": "2024-04"})
    assert response.status_code == 200
    cashflows = {row["asset_id"]: row for row in response.json()["asset_cashflows"]}

    asset_row = cashflows[str(asset.id)]
    assert asset_row["expense_czk"] == 500
    assert asset_row["income_czk"] == 120

    unlinked_row = cashflows[None]
    assert unlinked_row["expense_czk"] == 30
    assert unlinked_row["income_czk"] == 0
    assert unlinked_row["asset_name"] == "Bez majetku"


@requires_db
def test_self_parties_admin_only_and_round_trips(client, db_session, portfolio_id):
    from app.auth import hash_password
    from app.models import AppUser, Party

    db_session.add(
        AppUser(username="viewer3", password_hash=hash_password("s3cret!"), is_active=True, is_admin=False, allowed_agendas=[])
    )
    party = Party(name="Round Trip Party", kind="unknown")
    db_session.add(party)
    db_session.commit()

    admin_login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}
    viewer_login = client.post("/auth/login", json={"username": "viewer3", "password": "s3cret!"})
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['token']}"}

    forbidden = client.put(f"/portfolios/{portfolio_id}/self-parties", headers=viewer_headers, json={"party_ids": [str(party.id)]})
    assert forbidden.status_code == 403

    updated = client.put(f"/portfolios/{portfolio_id}/self-parties", headers=admin_headers, json={"party_ids": [str(party.id)]})
    assert updated.status_code == 200
    assert [row["name"] for row in updated.json()] == ["Round Trip Party"]

    listed = client.get(f"/portfolios/{portfolio_id}/self-parties", headers=admin_headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [str(party.id)]


@requires_db
def test_evaluation_includes_stock_investment_cashflow(client, db_session, portfolio_id):
    """Money moving into/out of the stock portfolio (buys/sells) is as real
    a cash movement as an Asset's costs, even though stocks aren't tracked
    as an Asset row - shown as its own figure in Pohyby hotovosti."""
    from app.models import StockTransaction

    db_session.add_all(
        [
            StockTransaction(
                portfolio_id=portfolio_id, traded_on=date(2024, 5, 3), movement_type="Nákup",
                instrument_name="Test Corp", ticker="TEST", quantity=Decimal("10"),
                amount_czk=Decimal("1000"), currency="CZK",
            ),
            StockTransaction(
                portfolio_id=portfolio_id, traded_on=date(2024, 5, 20), movement_type="Prodej",
                instrument_name="Test Corp", ticker="TEST", quantity=Decimal("-4"),
                amount_czk=Decimal("450"), currency="CZK",
            ),
            # A "Tip" row (watchlist/hypothetical) must not count as a real cash movement.
            StockTransaction(
                portfolio_id=portfolio_id, traded_on=date(2024, 5, 10), movement_type="Tip",
                instrument_name="Ignored Corp", ticker="IGN", quantity=Decimal("5"),
                amount_czk=Decimal("999999"), currency="CZK",
            ),
        ]
    )
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    response = client.post("/evaluations/compute", headers=headers, params={"portfolio_id": str(portfolio_id), "period": "2024-05"})
    assert response.status_code == 200
    body = response.json()
    assert body["stock_expense_czk"] == 1000  # the buy
    assert body["stock_income_czk"] == 450  # the sell


@requires_db
def test_evaluation_interest_detail_matches_aggregate_and_excludes_correctly(client, db_session, portfolio_id):
    """The on-demand breakdown must sum to exactly the same totals stored on
    the MonthlyEvaluation aggregate, and must list only the classified
    (self<->external) movements - not the excluded self<->self/external<->
    external ones."""
    from app.models import LoanMovement, Party

    self_a = Party(name="Detail Self A", kind="unknown")
    self_d = Party(name="Detail Self D", kind="unknown")
    external_b = Party(name="Detail External B", kind="unknown")
    external_c = Party(name="Detail External C", kind="unknown")
    db_session.add_all([self_a, self_d, external_b, external_c])
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    client.put(f"/portfolios/{portfolio_id}/self-parties", headers=headers, json={"party_ids": [str(self_a.id), str(self_d.id)]})

    loan_terms = dict(movement_date=date(2024, 6, 1), planned_end_date=date(2025, 5, 31), interest_rate=Decimal("0.12"))
    db_session.add_all(
        [
            LoanMovement(portfolio_id=portfolio_id, lender_id=self_a.id, borrower_id=external_b.id, amount=Decimal("120000"), **loan_terms),
            LoanMovement(portfolio_id=portfolio_id, lender_id=external_c.id, borrower_id=self_a.id, amount=Decimal("60000"), **loan_terms),
            LoanMovement(portfolio_id=portfolio_id, lender_id=self_a.id, borrower_id=self_d.id, amount=Decimal("999999"), **loan_terms),
            LoanMovement(portfolio_id=portfolio_id, lender_id=external_b.id, borrower_id=external_c.id, amount=Decimal("999999"), **loan_terms),
        ]
    )
    db_session.commit()

    computed = client.post("/evaluations/compute", headers=headers, params={**params, "period": "2024-07"})
    assert computed.status_code == 200
    evaluation = computed.json()

    detail = client.get(f"/evaluations/{evaluation['id']}/interest-detail", headers=headers, params=params)
    assert detail.status_code == 200
    body = detail.json()

    assert len(body["received"]) == 1
    assert body["received"][0]["counterparty"] == "Detail External B"
    assert sum(row["interest_czk"] for row in body["received"]) == pytest.approx(evaluation["interest_received_czk"])

    assert len(body["paid"]) == 1
    assert body["paid"][0]["counterparty"] == "Detail External C"
    assert sum(row["interest_czk"] for row in body["paid"]) == pytest.approx(evaluation["interest_paid_czk"])

    # Cross-portfolio access is rejected, same as every other portfolio-scoped endpoint.
    from app.models import Portfolio

    other_portfolio = Portfolio(name="Jiny Subjekt Detail")
    db_session.add(other_portfolio)
    db_session.commit()
    wrong_scope = client.get(
        f"/evaluations/{evaluation['id']}/interest-detail", headers=headers, params={"portfolio_id": str(other_portfolio.id)}
    )
    assert wrong_scope.status_code == 404
