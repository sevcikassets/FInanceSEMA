"""Tests for the owners/asset-types/linked-Hypotéka/Zápůjčka->Půjčky feature:
the ensure_schema_upgrades() migration steps, and the new AssetType/Owner/
Asset CRUD endpoints.

All figures below are fictional test fixtures, not real financial data.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from .conftest import requires_db


@requires_db
def test_ensure_schema_upgrades_moves_zapujcka_assets_to_loan_movements(db_session, portfolio_id):
    """"Zápůjčka" (money lent OUT) must become a real Půjčky record, not an
    Asset - and the principal lives in own_funds on the source row (not
    borrowed_amount, which is the real bug this whole feature traces back
    to: the old code always read borrowed_amount as principal, so this
    asset's interest projection silently computed nothing)."""
    from app.main import ensure_schema_upgrades
    from app.models import Asset, AssetType, LoanMovement, Party

    owner = Party(name="ŠEVČÍK ASSETS", kind="owner")
    db_session.add(owner)
    db_session.flush()
    db_session.add(
        Asset(
            portfolio_id=portfolio_id,
            code="001/26",
            name="Fond Spilberk",
            asset_type="Zápůjčka",
            owner_id=owner.id,
            own_funds=Decimal("4000000"),
            interest_rate=Decimal("0.095"),
            borrowed_from=date(2026, 5, 2),
            borrowed_to=date(2028, 8, 2),
        )
    )
    db_session.commit()

    ensure_schema_upgrades()

    assert db_session.scalar(select(Asset).where(Asset.portfolio_id == portfolio_id, Asset.code == "001/26")) is None
    movement = db_session.scalar(select(LoanMovement).where(LoanMovement.portfolio_id == portfolio_id))
    assert movement is not None
    assert movement.amount == Decimal("4000000")
    assert movement.interest_rate == Decimal("0.095")
    assert movement.movement_date == date(2026, 5, 2)
    assert movement.planned_end_date == date(2028, 8, 2)
    assert db_session.get(Party, movement.lender_id).name == "ŠEVČÍK ASSETS"
    assert db_session.get(Party, movement.borrower_id).name == "Fond Spilberk"
    # No orphaned "Zápůjčka" AssetType should ever get created - the move
    # happens before the type-dictionary backfill sees this row.
    assert db_session.scalar(select(AssetType).where(AssetType.portfolio_id == portfolio_id, AssetType.name == "Zápůjčka")) is None

    # ensure_schema_upgrades() runs DDL (ALTER TABLE) in its own connection,
    # which needs an exclusive table lock - db_session must release the
    # ACCESS SHARE lock its still-open read transaction is holding first, or
    # the DDL below blocks forever waiting on it.
    db_session.commit()

    # Idempotent: rerunning must not duplicate the movement.
    ensure_schema_upgrades()
    assert db_session.scalar(select(Asset).where(Asset.portfolio_id == portfolio_id).order_by(Asset.code)) is None
    all_movements = db_session.scalars(select(LoanMovement).where(LoanMovement.portfolio_id == portfolio_id)).all()
    assert len(all_movements) == 1


@requires_db
def test_ensure_schema_upgrades_backfills_asset_types_from_remaining_text(db_session, portfolio_id):
    from app.main import ensure_schema_upgrades
    from app.models import Asset, AssetType

    db_session.add(Asset(portfolio_id=portfolio_id, code="B-1", name="Byt A", asset_type="Byt", total_value=Decimal("1000000")))
    db_session.add(Asset(portfolio_id=portfolio_id, code="N-1", name="Nemovitost A", asset_type="Nemovitost"))
    db_session.commit()

    ensure_schema_upgrades()

    byt_type = db_session.scalar(select(AssetType).where(AssetType.portfolio_id == portfolio_id, AssetType.name == "Byt"))
    nemovitost_type = db_session.scalar(select(AssetType).where(AssetType.portfolio_id == portfolio_id, AssetType.name == "Nemovitost"))
    assert byt_type.calculation_mode == "none"
    assert nemovitost_type.calculation_mode == "none"

    byt_asset = db_session.scalar(select(Asset).where(Asset.portfolio_id == portfolio_id, Asset.code == "B-1"))
    assert byt_asset.asset_type_id == byt_type.id


@requires_db
def test_ensure_schema_upgrades_splits_combined_byt_and_loan_into_linked_hypoteka(db_session, portfolio_id):
    """Shaped exactly like the real "004/26" row: a Byt with both valuation
    fields AND full mortgage terms on the same row - must split into two
    rows, original keeps only the valuation."""
    from app.main import ensure_schema_upgrades
    from app.models import Asset, AssetType

    db_session.add(
        Asset(
            portfolio_id=portfolio_id,
            code="004/26",
            name="Byt Brno Ghegova 3",
            asset_type="Byt",
            total_value=Decimal("8506500"),
            own_funds=Decimal("4000500"),
            borrowed_amount=Decimal("4506000"),
            interest_rate=Decimal("0.0489"),
            loan_years=Decimal("15"),
            borrowed_from=date(2026, 7, 1),
            borrowed_to=date(2041, 6, 30),
            source_row=42,
        )
    )
    db_session.commit()

    ensure_schema_upgrades()

    rows = db_session.scalars(select(Asset).where(Asset.portfolio_id == portfolio_id).order_by(Asset.code)).all()
    assert [r.code for r in rows] == ["004/26", "004/26-HYP"]
    original, linked = rows
    assert original.total_value == Decimal("8506500")
    assert original.own_funds == Decimal("4000500")
    assert original.borrowed_amount is None
    assert original.interest_rate is None
    assert original.borrowed_from is None

    assert linked.linked_asset_id == original.id
    assert linked.borrowed_amount == Decimal("4506000")
    assert linked.interest_rate == Decimal("0.0489")
    assert linked.loan_years == Decimal("15")
    assert linked.borrowed_from == date(2026, 7, 1)
    assert linked.borrowed_to == date(2041, 6, 30)
    hypoteka_type = db_session.get(AssetType, linked.asset_type_id)
    assert hypoteka_type.name == "Hypotéka"
    assert hypoteka_type.calculation_mode == "debt_interest"

    # ensure_schema_upgrades()'s DDL needs an exclusive table lock -
    # db_session must release its open read transaction's lock first, or the
    # second call below blocks forever (see the same fix/comment in
    # test_ensure_schema_upgrades_moves_zapujcka_assets_to_loan_movements).
    db_session.commit()

    # Idempotent: rerunning must not split again.
    ensure_schema_upgrades()
    rows_after = db_session.scalars(select(Asset).where(Asset.portfolio_id == portfolio_id)).all()
    assert len(rows_after) == 2


@requires_db
def test_ensure_schema_upgrades_does_not_split_asset_missing_loan_terms(db_session, portfolio_id):
    """Regression for real row "003/26": borrowed_amount is set but
    interest_rate/loan_years/borrowed_from were never filled in the source
    spreadsheet - must stay a single "none"-mode row forever, exactly like
    project_annual_interest's own "missing inputs -> no projection" gate."""
    from app.main import ensure_schema_upgrades
    from app.models import Asset

    db_session.add(
        Asset(
            portfolio_id=portfolio_id,
            code="003/26",
            name="Byt Brno Rybářská 3",
            asset_type="Byt",
            total_value=Decimal("14761000"),
            own_funds=Decimal("7811000"),
            borrowed_amount=Decimal("6950000"),
            source_row=17,
        )
    )
    db_session.commit()

    ensure_schema_upgrades()

    rows = db_session.scalars(select(Asset).where(Asset.portfolio_id == portfolio_id)).all()
    assert len(rows) == 1
    assert rows[0].borrowed_amount == Decimal("6950000")


@requires_db
def test_ensure_schema_upgrades_does_not_split_zero_borrowed_amount(db_session, portfolio_id):
    """Regression for real row "001/24": borrowed_amount=0 (not NULL) with
    full-looking loan fields would otherwise spawn a pointless zero-value
    Hypotéka row."""
    from app.main import ensure_schema_upgrades
    from app.models import Asset

    db_session.add(
        Asset(
            portfolio_id=portfolio_id,
            code="001/24",
            name="Byt všetuly 93",
            asset_type="Byt",
            total_value=Decimal("6120000"),
            own_funds=Decimal("6120000"),
            borrowed_amount=Decimal("0"),
            interest_rate=Decimal("0.05"),
            borrowed_from=date(2024, 1, 1),
            source_row=5,
        )
    )
    db_session.commit()

    ensure_schema_upgrades()

    rows = db_session.scalars(select(Asset).where(Asset.portfolio_id == portfolio_id)).all()
    assert len(rows) == 1


@requires_db
def test_ensure_schema_upgrades_never_splits_manually_created_asset(db_session, portfolio_id):
    """A manually created Asset (via the new CRUD form, source_row=None) that
    deliberately keeps full loan fields inline must never get silently split
    apart on the next restart - only Excel-imported rows are candidates."""
    from app.main import ensure_schema_upgrades
    from app.models import Asset

    db_session.add(
        Asset(
            portfolio_id=portfolio_id,
            code="MANUAL-1",
            name="Rucne pridany byt",
            total_value=Decimal("5000000"),
            own_funds=Decimal("1000000"),
            borrowed_amount=Decimal("4000000"),
            interest_rate=Decimal("0.05"),
            loan_years=Decimal("20"),
            borrowed_from=date(2026, 1, 1),
            source_row=None,
        )
    )
    db_session.commit()

    ensure_schema_upgrades()

    rows = db_session.scalars(select(Asset).where(Asset.portfolio_id == portfolio_id)).all()
    assert len(rows) == 1


@requires_db
def test_asset_type_crud_admin_only_write_scoped_read_and_in_use_guard(client, db_session, portfolio_id):
    from app.auth import hash_password
    from app.models import Asset, AppUser, PortfolioAccess

    db_session.add(
        AppUser(username="reader", password_hash=hash_password("s3cret!"), is_active=True, is_admin=False, allowed_agendas=[])
    )
    db_session.add(PortfolioAccess(username="reader", portfolio_id=portfolio_id, allowed_agendas=["asset_types"]))
    db_session.commit()

    admin_login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}
    reader_login = client.post("/auth/login", json={"username": "reader", "password": "s3cret!"})
    reader_headers = {"Authorization": f"Bearer {reader_login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    created = client.post(
        "/assets/asset-types", headers=admin_headers, params=params, json={"name": "Hypotéka", "calculation_mode": "debt_interest"}
    )
    assert created.status_code == 200
    type_id = created.json()["id"]

    duplicate = client.post("/assets/asset-types", headers=admin_headers, params=params, json={"name": "Hypotéka"})
    assert duplicate.status_code == 409

    listed = client.get("/assets/asset-types", headers=reader_headers, params=params)
    assert listed.status_code == 200
    assert listed.json()[0]["calculation_mode"] == "debt_interest"

    forbidden_create = client.post("/assets/asset-types", headers=reader_headers, params=params, json={"name": "Jina"})
    assert forbidden_create.status_code == 403

    updated = client.put(
        f"/assets/asset-types/{type_id}",
        headers=admin_headers,
        params=params,
        json={"name": "Hypotéka", "calculation_mode": "none", "required_fields": ["borrowed_amount"]},
    )
    assert updated.status_code == 200
    assert updated.json()["calculation_mode"] == "none"

    invalid_field = client.put(
        f"/assets/asset-types/{type_id}",
        headers=admin_headers,
        params=params,
        json={"name": "Hypotéka", "required_fields": ["not_a_real_field"]},
    )
    assert invalid_field.status_code == 400

    db_session.add(Asset(portfolio_id=portfolio_id, code="X-1", name="X", asset_type_id=type_id))
    db_session.commit()
    blocked_delete = client.delete(f"/assets/asset-types/{type_id}", headers=admin_headers)
    assert blocked_delete.status_code == 409

    db_session.execute(Asset.__table__.delete().where(Asset.code == "X-1"))
    db_session.commit()
    deleted = client.delete(f"/assets/asset-types/{type_id}", headers=admin_headers)
    assert deleted.status_code == 200


@requires_db
def test_payer_crud_any_user_read_admin_write_and_conflicts(client, db_session, portfolio_id):
    from app.auth import hash_password
    from app.models import AppUser, AssetCost, Party

    # Granted nothing at all - proves payers is unconditionally readable.
    db_session.add(
        AppUser(username="nobody", password_hash=hash_password("s3cret!"), is_active=True, is_admin=False, allowed_agendas=[])
    )
    db_session.commit()

    admin_login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}
    nobody_login = client.post("/auth/login", json={"username": "nobody", "password": "s3cret!"})
    nobody_headers = {"Authorization": f"Bearer {nobody_login.json()['token']}"}

    created = client.post("/parties/payers", headers=admin_headers, json={"name": "Martin"})
    assert created.status_code == 200
    payer_id = created.json()["id"]

    listed = client.get("/parties/payers", headers=nobody_headers)
    assert listed.status_code == 200
    assert [row["name"] for row in listed.json()] == ["Martin"]

    forbidden_create = client.post("/parties/payers", headers=nobody_headers, json={"name": "Jinak"})
    assert forbidden_create.status_code == 403

    duplicate_payer = client.post("/parties/payers", headers=admin_headers, json={"name": "Martin"})
    assert duplicate_payer.status_code == 409

    # Reusing an existing "unknown"-kind Party name upgrades it instead of conflicting.
    db_session.add(Party(name="Existing Unknown", kind="unknown"))
    db_session.commit()
    upgraded = client.post("/parties/payers", headers=admin_headers, json={"name": "Existing Unknown"})
    assert upgraded.status_code == 200

    # A name already used by a different kind (e.g. owner) genuinely conflicts.
    db_session.add(Party(name="Existing Owner", kind="owner"))
    db_session.commit()
    conflict = client.post("/parties/payers", headers=admin_headers, json={"name": "Existing Owner"})
    assert conflict.status_code == 409

    db_session.add(AssetCost(portfolio_id=portfolio_id, item="Test", payer_id=payer_id))
    db_session.commit()
    blocked_delete = client.delete(f"/parties/payers/{payer_id}", headers=admin_headers)
    assert blocked_delete.status_code == 409

    db_session.execute(AssetCost.__table__.delete().where(AssetCost.item == "Test"))
    db_session.commit()
    deleted = client.delete(f"/parties/payers/{payer_id}", headers=admin_headers)
    assert deleted.status_code == 200


@requires_db
def test_asset_crud_full_lifecycle_and_validation(client, db_session, portfolio_id):
    from app.models import Portfolio

    other_portfolio = Portfolio(name="Jiny Subjekt")
    db_session.add(other_portfolio)
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    created = client.post("/assets", headers=headers, params=params, json={"code": "T-1", "name": "Testovaci byt", "total_value": "1000000"})
    assert created.status_code == 200
    asset_id = created.json()["id"]
    assert created.json()["net_worth_contribution"] == 1000000.0

    duplicate_code = client.post("/assets", headers=headers, params=params, json={"code": "T-1", "name": "Jiny"})
    assert duplicate_code.status_code == 409

    foreign_asset = client.post(
        "/assets", headers=headers, params={"portfolio_id": str(other_portfolio.id)}, json={"code": "F-1", "name": "Cizi"}
    )
    assert foreign_asset.status_code == 200
    foreign_asset_id = foreign_asset.json()["id"]

    cross_portfolio_link = client.put(
        f"/assets/{asset_id}", headers=headers, params=params, json={"code": "T-1", "name": "Testovaci byt", "linked_asset_id": foreign_asset_id}
    )
    assert cross_portfolio_link.status_code == 404

    self_link = client.put(
        f"/assets/{asset_id}", headers=headers, params=params, json={"code": "T-1", "name": "Testovaci byt", "linked_asset_id": asset_id}
    )
    assert self_link.status_code == 400

    # owner is free text (get-or-create against Party, kind="owner") - not a
    # dictionary FK pick like asset_type_id/linked_asset_id above, since
    # asset ownership reverted to plain free text (the "Vlastníci" registry
    # was redirected to manage cost payers instead, see "Plátci").
    from app.models import Party

    owner_set = client.put(
        f"/assets/{asset_id}",
        headers=headers,
        params=params,
        json={"code": "T-1", "name": "Testovaci byt", "owner": "Nový vlastník"},
    )
    assert owner_set.status_code == 200
    assert owner_set.json()["owner"] == "Nový vlastník"
    assert db_session.query(Party).filter_by(name="Nový vlastník", kind="owner").count() == 1

    updated = client.put(f"/assets/{asset_id}", headers=headers, params=params, json={"code": "T-1", "name": "Upraveny nazev", "total_value": "2000000"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Upraveny nazev"

    cost_created = client.post("/assets/costs", headers=headers, params=params, json={"item": "Test", "asset_id": asset_id})
    assert cost_created.status_code == 200
    blocked_delete = client.delete(f"/assets/{asset_id}", headers=headers, params=params)
    assert blocked_delete.status_code == 409
    client.delete(f"/assets/costs/{cost_created.json()['id']}", headers=headers, params=params)

    deleted = client.delete(f"/assets/{asset_id}", headers=headers, params=params)
    assert deleted.status_code == 200

    from app.auth import hash_password
    from app.models import AppUser, PortfolioAccess

    db_session.add(
        AppUser(username="viewer_asset_only", password_hash=hash_password("s3cret!"), is_active=True, is_admin=False, allowed_agendas=[])
    )
    # Granted "costs" but not "assets" - proves the two agendas are checked independently.
    db_session.add(PortfolioAccess(username="viewer_asset_only", portfolio_id=portfolio_id, allowed_agendas=["costs"]))
    db_session.commit()
    viewer_login = client.post("/auth/login", json={"username": "viewer_asset_only", "password": "s3cret!"})
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['token']}"}
    forbidden = client.post("/assets", headers=viewer_headers, params=params, json={"code": "T-2", "name": "X"})
    assert forbidden.status_code == 403


@requires_db
def test_computed_interest_plan_requires_debt_interest_mode(client, db_session, portfolio_id):
    """The key regression-proofing case: an asset with full loan fields but
    calculation_mode="none" must NOT get an interest projection, unlike the
    old unconditional behavior."""
    from app.models import Asset, AssetType

    none_type = AssetType(portfolio_id=portfolio_id, name="Byt bez vypoctu", calculation_mode="none")
    db_session.add(none_type)
    db_session.flush()
    asset = Asset(
        portfolio_id=portfolio_id,
        code="N-2",
        name="Byt",
        asset_type_id=none_type.id,
        borrowed_amount=Decimal("1000000"),
        interest_rate=Decimal("0.05"),
        borrowed_from=date(2024, 1, 1),
        loan_years=Decimal("10"),
    )
    db_session.add(asset)
    db_session.commit()
    asset_id = str(asset.id)

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    params = {"portfolio_id": str(portfolio_id)}

    response = client.get(f"/assets/{asset_id}/interest-projection", headers=headers, params=params)
    assert response.status_code == 200
    assert response.json()["computed_plan"] == {}
    assert response.json()["total_computed"] == 0

    schedule_response = client.get(f"/assets/{asset_id}/payment-schedule", headers=headers, params=params)
    assert schedule_response.status_code == 200
    # Payment schedule is NOT gated by calculation_mode (it's a pure function
    # of the loan fields, usable regardless of type) - only the net-worth/
    # interest-plan logic branches on mode. Since every loan field is
    # present, it still returns a real schedule.
    assert len(schedule_response.json()) == 120


@requires_db
def test_summary_assets_total_nets_debt_interest_contribution(client, db_session, portfolio_id):
    from app.models import Asset, AssetType

    debt_type = AssetType(portfolio_id=portfolio_id, name="Hypotéka test", calculation_mode="debt_interest")
    db_session.add(debt_type)
    db_session.flush()
    db_session.add(Asset(portfolio_id=portfolio_id, code="S-1", name="Byt", total_value=Decimal("500000")))
    db_session.add(Asset(portfolio_id=portfolio_id, code="S-2", name="Hypotéka", asset_type_id=debt_type.id, borrowed_amount=Decimal("1000000")))
    db_session.commit()

    login = client.post("/auth/login", json={"username": "admin", "password": "finance"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    response = client.get("/summary", headers=headers, params={"portfolio_id": str(portfolio_id)})
    assert response.status_code == 200
    assert response.json()["assets_total"] == -500000.0
