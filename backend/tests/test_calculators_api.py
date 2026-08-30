"""Tests for the /api/calculators/* HTTP routes.

Sample inputs mirror the worked examples already covered by
`test_rorc.py`, `test_kelly.py`, and `test_cost_basis.py`, so the expected
outputs here are known-good, not new assertions.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_rorc_endpoint_matches_spreadsheet_worked_example():
    response = client.post(
        "/api/calculators/rorc",
        json={
            "net_credit_per_share": "0.8259",
            "risk_capital_per_share": "216.6741",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert round(float(body["rorc"]), 7) == 0.0038117


def test_arorc_endpoint_matches_spreadsheet_worked_example():
    response = client.post(
        "/api/calculators/arorc",
        json={"rorc": "0.0038117153826876396", "days_to_expiration": 7},
    )
    assert response.status_code == 200
    body = response.json()
    assert round(float(body["arorc"]), 8) == 0.19875373


def test_kelly_endpoint_basic_case():
    response = client.post(
        "/api/calculators/kelly",
        json={"probability_of_winning": "0.7", "avg_win": "2", "avg_loss": "1"},
    )
    assert response.status_code == 200
    assert response.json()["kelly"] == "0.55"


def test_cost_basis_endpoint_first_entry():
    response = client.post(
        "/api/calculators/cost-basis",
        json={
            "prior_running_basis": "0",
            "prior_running_shares": "0",
            "total_amount": "1000.00",
            "shares": "100",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["running_basis"] == "1000.00"
    assert body["running_shares"] == "100"
    assert body["basis_per_share"] == "10.00"
