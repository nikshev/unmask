# verifies: FR-001-15
"""Фікстурне джерело даних: семантика курсорів, журнал викликів, ін'єкція збоїв."""

from pathlib import Path

import pytest

from unmask.ingest.budget import FakeClock
from unmask.ingest.rpc.fixture import FailAfter, FailFor, FixtureRpcSource
from unmask.ingest.rpc.protocol import (
    RpcRateLimited,
    RpcTimeout,
    RpcUnavailable,
)

SCENARIO = Path(__file__).parent / "fixtures" / "scenarios" / "minimal"
WALLET = "WALLET1"
# Дедлайн фікстурне джерело не перевіряє (його реалізує T-015); передається як є.
DEADLINE = None


def _source(**kwargs) -> FixtureRpcSource:
    return FixtureRpcSource(SCENARIO, **kwargs)


def _sigs(entries) -> list[str]:
    return [e["signature"] for e in entries]


def test_before_returns_strictly_older_entries():
    src = _source()
    got = src.get_signatures_for_address(
        WALLET, before="sig3", until=None, limit=1000, deadline=DEADLINE
    )
    assert _sigs(got) == ["sig2", "sig1"]  # від найновішого; сам sig3 не входить
    oldest = src.get_signatures_for_address(
        WALLET, before="sig1", until=None, limit=1000, deadline=DEADLINE
    )
    assert oldest == []


def test_until_returns_strictly_newer_entries():
    src = _source()
    got = src.get_signatures_for_address(
        WALLET, before=None, until="sig3", limit=1000, deadline=DEADLINE
    )
    assert _sigs(got) == ["sig5", "sig4"]  # від найновішого; сам sig3 не входить
    newest = src.get_signatures_for_address(
        WALLET, before=None, until="sig5", limit=1000, deadline=DEADLINE
    )
    assert newest == []
    window = src.get_signatures_for_address(
        WALLET, before="sig5", until="sig2", limit=1000, deadline=DEADLINE
    )
    assert _sigs(window) == ["sig4", "sig3"]


def test_limit_and_empty_list_at_end_of_history():
    src = _source()
    first = src.get_signatures_for_address(
        WALLET, before=None, until=None, limit=2, deadline=DEADLINE
    )
    assert _sigs(first) == ["sig5", "sig4"]
    # перегортання сторінками через before доходить до кінця й повертає порожній список
    pages = []
    cursor = None
    while True:
        page = src.get_signatures_for_address(
            WALLET, before=cursor, until=None, limit=2, deadline=DEADLINE
        )
        if not page:
            break
        pages.append(_sigs(page))
        cursor = page[-1]["signature"]
    assert pages == [["sig5", "sig4"], ["sig3", "sig2"], ["sig1"]]


def test_get_transactions_preserves_argument_order_and_none_for_unknown():
    src = _source()
    got = src.get_transactions(["sig2", "sig1", "nope"], deadline=DEADLINE)
    assert len(got) == 3
    assert got[0] is None  # у файлі явний null
    assert got[1] is not None and got[1]["transaction"]["signatures"] == ["sig1"]
    assert got[2] is None  # підпису немає у файлі


def test_unknown_address_yields_empty_history_not_error():
    src = _source()
    assert src.get_signatures_for_address(
        "STRANGER", before=None, until=None, limit=10, deadline=DEADLINE
    ) == []
    assert src.get_signatures_for_address(
        "STRANGER", before="whatever", until=None, limit=10, deadline=DEADLINE
    ) == []
    assert src.get_token_accounts_by_owner("STRANGER", deadline=DEADLINE) == []
    assert src.get_account_info("NOACCOUNT", deadline=DEADLINE) is None
    assert src.get_account_info("STRANGER", deadline=DEADLINE) is None
    mint = src.get_account_info("MINT1", deadline=DEADLINE)
    assert mint is not None and mint["data"]["parsed"]["type"] == "mint"


def test_fail_after_raises_configured_exception_on_nth_call():
    exc = RpcRateLimited(retry_after=1.5)
    src = _source(failures=[FailAfter(3, exc)])
    src.get_account_info("MINT1", deadline=DEADLINE)  # виклик 1
    src.get_token_accounts_by_owner(WALLET, deadline=DEADLINE)  # виклик 2
    with pytest.raises(RpcRateLimited) as excinfo:  # виклик 3
        src.get_transactions(["sig1"], deadline=DEADLINE)
    assert excinfo.value.retry_after == 1.5
    with pytest.raises(RpcRateLimited):  # ліміт не відпускає
        src.get_account_info("MINT1", deadline=DEADLINE)


def test_fail_for_key_raises_limited_number_of_times():
    src = _source(failures=[FailFor("WALLET1", RpcUnavailable("boom"), times=2)])
    for _ in range(2):
        with pytest.raises(RpcUnavailable):
            src.get_signatures_for_address(
                WALLET, before=None, until=None, limit=10, deadline=DEADLINE
            )
    # інша адреса не зачеплена, а після двох збоїв ця теж відпускає
    assert src.get_signatures_for_address(
        "STRANGER", before=None, until=None, limit=10, deadline=DEADLINE
    ) == []
    assert len(src.get_signatures_for_address(
        WALLET, before=None, until=None, limit=10, deadline=DEADLINE
    )) == 5
    # FailFor спрацьовує й на підпис усередині пакета
    tx_src = _source(failures=[FailFor("sig1", RpcTimeout("slow"), times=1)])
    with pytest.raises(RpcTimeout):
        tx_src.get_transactions(["sig2", "sig1"], deadline=DEADLINE)
    assert tx_src.get_transactions(["sig1"], deadline=DEADLINE)[0] is not None


def test_calls_log_records_method_and_params():
    src = _source()
    assert src.calls == []
    src.get_account_info("MINT1", deadline=DEADLINE)
    src.get_signatures_for_address(WALLET, before="sig3", until=None, limit=7, deadline=DEADLINE)
    src.get_transactions(["sig1", "sig2"], deadline=DEADLINE)
    src.get_token_accounts_by_owner(WALLET, deadline=DEADLINE)
    assert src.calls == [
        ("getAccountInfo", {"address": "MINT1"}),
        ("getSignaturesForAddress",
         {"address": WALLET, "before": "sig3", "until": None, "limit": 7}),
        ("getTransaction", {"signatures": ["sig1", "sig2"]}),
        ("getTokenAccountsByOwner", {"owner": WALLET}),
    ]


def test_failed_call_is_still_logged_and_counted():
    src = _source(failures=[FailAfter(2, RpcUnavailable("down"))])
    src.get_account_info("MINT1", deadline=DEADLINE)
    with pytest.raises(RpcUnavailable):
        src.get_account_info("MINT1", deadline=DEADLINE)
    assert [m for m, _ in src.calls] == ["getAccountInfo", "getAccountInfo"]


def test_fake_clock_advances_on_every_call():
    clock = FakeClock(advance_per_call=2.5)
    src = _source(clock=clock)
    assert clock.monotonic() == 0.0
    src.get_account_info("MINT1", deadline=DEADLINE)
    src.get_transactions(["sig1"], deadline=DEADLINE)
    assert clock.monotonic() == 5.0


def test_cursor_outside_history_is_a_fixture_error():
    src = _source()
    with pytest.raises(ValueError):
        src.get_signatures_for_address(
            WALLET, before="not-in-history", until=None, limit=10, deadline=DEADLINE
        )


def test_name_identifies_scenario():
    assert _source().name == "fixture:minimal"
