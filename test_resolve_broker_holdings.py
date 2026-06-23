"""Tests for broker holdings resolution (present-balance vs inquire-balance vs ccnl)."""

from __future__ import annotations

from session_manager import (
    _parse_inquire_balance_holdings,
    resolve_broker_holdings,
)


class _BalanceClient:
    def __init__(self, *, balance_payloads: dict[str, dict], ccnl_rows: list[dict] | None = None):
        self.balance_payloads = balance_payloads
        self.ccnl_rows = ccnl_rows or []

    def fetch_overseas_inquire_balance(self, ovrs_excg_cd: str, tr_crcy_cd: str = "USD"):
        return self.balance_payloads.get(ovrs_excg_cd, {"output1": []})

    def fetch_overseas_order_ccnl(self, *_args, **_kwargs):
        return self.ccnl_rows


def test_inquire_balance_parses_ovrs_cblc_qty() -> None:
    payload = {
        "output1": [
            {"ovrs_pdno": "TSM", "ovrs_cblc_qty": "15"},
            {"ovrs_pdno": "TSLA", "ovrs_cblc_qty": "68"},
        ]
    }
    holdings = _parse_inquire_balance_holdings(payload, ["TSM", "TSLA", "AAPL"])
    assert holdings == {"TSM": 15, "TSLA": 68, "AAPL": 0}


def test_resolve_prefers_inquire_balance_over_ccnl_on_vts() -> None:
    present = {
        "output1": [
            {
                "pdno": "TSM",
                "cblc_qty13": "0.00000000",
                "evlu_pfls_rt1": "-2.5",
            }
        ],
        "output3": {"tot_asst_amt": "100000"},
    }
    client = _BalanceClient(
        balance_payloads={
            "NASD": {"output1": [{"ovrs_pdno": "TSLA", "ovrs_cblc_qty": "68"}]},
            "NYSE": {"output1": [{"ovrs_pdno": "TSM", "ovrs_cblc_qty": "15"}]},
            "AMEX": {"output1": []},
        },
        ccnl_rows=[
            {"pdno": "TSM", "sll_buy_dvsn_cd": "02", "ccld_qty": "73"},
            {"pdno": "TSLA", "sll_buy_dvsn_cd": "02", "ccld_qty": "67"},
        ],
    )
    holdings = resolve_broker_holdings(client, ["TSM", "TSLA"], present)
    assert holdings["TSM"] == 15
    assert holdings["TSLA"] == 68


def test_resolve_uses_present_balance_when_qty_populated() -> None:
    present = {
        "output1": [{"pdno": "TSM", "ovrs_stck_tot_qty": "20"}],
    }
    client = _BalanceClient(
        balance_payloads={"NYSE": {"output1": [{"ovrs_pdno": "TSM", "ovrs_cblc_qty": "15"}]}},
    )
    holdings = resolve_broker_holdings(client, ["TSM"], present)
    assert holdings["TSM"] == 20
