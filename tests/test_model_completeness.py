# verifies: FR-001-06, FR-001-09, FR-001-10
"""Модель результату збору: похідний статус повноти, доказовість переказу, ключі порядку.

Критична задача (T-003): помилка тут не ламає збірку, а тихо видає неповний
результат за повний (принцип V). Тому окрім щасливого шляху перевіряється, що
суперечливий стан неможливо сконструювати жодним прямим шляхом.
"""

import dataclasses
import inspect
import random

import pytest

from unmask.ingest.model import (
    AddressType,
    Asset,
    Buyer,
    BuyersCompleteness,
    CacheInvariantError,
    Completeness,
    CompletenessStatus,
    IngestResult,
    MissingHistory,
    MissingReason,
    RejectKind,
    Rejection,
    RunMetadata,
    Spend,
    Transfer,
    UnexpandedNode,
    UnexpandedReason,
    buyer_sort_key,
    transfer_sort_key,
)

W1 = "4Nd1mYtq3oG7Q2bKjv9b8yXbH9QJm6N3s1R5o2nZ8kPq"
W2 = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
W3 = "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj"
W4 = "2wmVCSfPxGPjrnMMn7rchp4uaeoTqN39mXFC2zhPdri9"
MINT = "So11111111111111111111111111111111111111112"
SIG_A = "5" + "A" * 87
SIG_B = "5" + "B" * 87


def _missing(wallet=W1, depth=1, reason=MissingReason.TIMEOUT, detail="getSignaturesForAddress"):
    return MissingHistory(wallet=wallet, depth=depth, reason=reason, detail=detail)


def _buyers_ok():
    return BuyersCompleteness(complete=True, reason=None, detail="")


def _buyers_truncated(reason=MissingReason.BUDGET_EXHAUSTED):
    return BuyersCompleteness(complete=False, reason=reason, detail="cursor=" + SIG_A)


TRANSFER_KW = dict(
    signature=SIG_A,
    slot=100,
    block_time=1_700_000_000,
    instruction_path="0",
    sender=W2,
    receiver=W1,
    asset="sol",
    amount=1_000_000,
    decimals=None,
    depth=1,
)


def _transfer(**changes):
    return Transfer(**{**TRANSFER_KW, **changes})


def _buyer(wallet=W1, rank=1, slot=100, sig=SIG_A, **changes):
    kw = dict(
        wallet=wallet,
        rank=rank,
        first_buy_signature=sig,
        first_buy_slot=slot,
        first_buy_time=1_700_000_000,
        received_amount=5_000,
        spent=(Spend(asset=Asset.SOL, amount=10_000),),
        programs=("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",),
        address_type=AddressType.WALLET,
    )
    kw.update(changes)
    return Buyer(**kw)


def _metadata(wallets_analyzed=0):
    return RunMetadata(
        mint=MINT,
        analyzed_at=1_700_000_100,
        wallets_analyzed=wallets_analyzed,
        config_version=1,
        first_buyers_n=300,
        funding_depth=2,
        counterparty_threshold=200,
        max_signatures_per_wallet=300,
        collect_spl_inbound=True,
        time_budget_seconds=40.0,
        elapsed_seconds=1.5,
        rpc_calls=3,
        transactions_scanned=2,
        source="fixture:basic",
        resumed=False,
        served_from_cache=False,
    )


# --- Completeness.derive: статус повноти лише похідний -------------------------


def test_derive_incomplete_when_missing_nonempty():
    c = Completeness.derive([_missing()], _buyers_ok())

    assert c.status is CompletenessStatus.INCOMPLETE
    assert len(c.missing) == 1


@pytest.mark.parametrize("reason", list(MissingReason))
def test_derive_incomplete_for_every_missing_reason(reason):
    c = Completeness.derive([_missing(reason=reason)], _buyers_ok())

    assert c.status is CompletenessStatus.INCOMPLETE


def test_derive_incomplete_when_buyers_truncated():
    c = Completeness.derive([], _buyers_truncated())

    assert c.status is CompletenessStatus.INCOMPLETE
    assert c.missing == ()


def test_derive_incomplete_when_both_missing_and_buyers_truncated():
    c = Completeness.derive([_missing()], _buyers_truncated())

    assert c.status is CompletenessStatus.INCOMPLETE


def test_derive_complete_only_when_both_clean():
    c = Completeness.derive([], _buyers_ok())

    assert c.status is CompletenessStatus.COMPLETE
    assert c.missing == ()
    assert c.buyers.complete is True


def test_derive_accepts_any_empty_iterable_as_clean():
    assert Completeness.derive((), _buyers_ok()).status is CompletenessStatus.COMPLETE
    assert Completeness.derive(iter(()), _buyers_ok()).status is CompletenessStatus.COMPLETE


def test_derive_freezes_missing_so_later_mutation_of_input_cannot_flip_status():
    source = []
    c = Completeness.derive(source, _buyers_ok())
    source.append(_missing())

    assert isinstance(c.missing, tuple)
    assert c.status is CompletenessStatus.COMPLETE

    source2 = [_missing()]
    c2 = Completeness.derive(source2, _buyers_ok())
    source2.clear()

    assert c2.status is CompletenessStatus.INCOMPLETE
    assert len(c2.missing) == 1


def test_derive_orders_missing_by_depth_then_wallet():
    entries = [_missing(W3, 2), _missing(W2, 1), _missing(W4, 2), _missing(W1, 1)]
    c = Completeness.derive(entries, _buyers_ok())

    assert [(m.depth, m.wallet) for m in c.missing] == sorted((m.depth, m.wallet) for m in entries)


def test_derive_rejects_duplicate_wallet_reason_pair():
    with pytest.raises(ValueError):
        Completeness.derive([_missing(detail="a"), _missing(detail="b")], _buyers_ok())


def test_derive_allows_same_wallet_with_different_reasons():
    c = Completeness.derive(
        [_missing(reason=MissingReason.TIMEOUT), _missing(reason=MissingReason.CORRUPT_DATA)],
        _buyers_ok(),
    )

    assert len(c.missing) == 2


def test_derive_rejects_non_model_items():
    with pytest.raises(TypeError):
        Completeness.derive([{"wallet": W1, "reason": "timeout"}], _buyers_ok())
    with pytest.raises(TypeError):
        Completeness.derive([], {"complete": True})
    with pytest.raises(TypeError):
        Completeness.derive([], True)


def test_direct_construction_of_contradictory_completeness_rejected():
    # status — не поле: його неможливо передати, ані зберегти суперечливим.
    assert "status" not in {f.name for f in dataclasses.fields(Completeness)}
    assert "status" not in inspect.signature(Completeness).parameters

    with pytest.raises(TypeError):
        Completeness(  # type: ignore[call-arg]
            status=CompletenessStatus.COMPLETE, missing=(_missing(),), buyers=_buyers_ok()
        )
    # Прямий конструктор узагалі закритий: єдиний вхід — derive.
    with pytest.raises(TypeError):
        Completeness(missing=(), buyers=_buyers_ok())
    with pytest.raises(TypeError):
        Completeness((_missing(),), _buyers_truncated())


def test_status_cannot_be_assigned_after_construction():
    c = Completeness.derive([_missing()], _buyers_ok())

    with pytest.raises(AttributeError):
        c.status = CompletenessStatus.COMPLETE  # type: ignore[misc]
    with pytest.raises(AttributeError):
        c.missing = ()  # type: ignore[misc]
    assert c.status is CompletenessStatus.INCOMPLETE


def test_status_follows_data_even_if_fields_are_forced():
    # Навіть обхід frozen не дає «застарілого» статусу: він обчислюється з даних.
    c = Completeness.derive([], _buyers_ok())
    object.__setattr__(c, "missing", (_missing(),))

    assert c.status is CompletenessStatus.INCOMPLETE


def test_replace_cannot_produce_contradiction():
    c = Completeness.derive([], _buyers_ok())
    c2 = dataclasses.replace(c, missing=(_missing(),))

    assert c2.status is CompletenessStatus.INCOMPLETE
    with pytest.raises(TypeError):
        dataclasses.replace(c, missing=({"wallet": W1},))


def test_complete_implies_missing_empty_and_buyers_complete_exhaustively():
    # Інваріант у обидва боки на всій решітці входів.
    for missing in ([], [_missing()], [_missing(W1), _missing(W2)]):
        for buyers in (_buyers_ok(), _buyers_truncated(), _buyers_truncated(MissingReason.RATE_LIMITED)):
            c = Completeness.derive(missing, buyers)
            is_complete = c.status is CompletenessStatus.COMPLETE
            assert is_complete == (len(c.missing) == 0 and c.buyers.complete)


# --- BuyersCompleteness: прапорець і причина узгоджені --------------------------


def test_buyers_complete_with_reason_is_rejected():
    with pytest.raises(ValueError):
        BuyersCompleteness(complete=True, reason=MissingReason.TIMEOUT, detail="")


def test_buyers_incomplete_without_reason_is_rejected():
    with pytest.raises(ValueError):
        BuyersCompleteness(complete=False, reason=None, detail="")


@pytest.mark.parametrize("flag", ["false", "true", 0, 1, None])
def test_buyers_complete_must_be_real_bool(flag):
    # "false" істинне в Python — без перевірки дало б хибне «повний».
    with pytest.raises(TypeError):
        BuyersCompleteness(complete=flag, reason=None, detail="")


def test_reason_strings_are_coerced_and_unknown_rejected():
    assert _missing(reason="rate_limited").reason is MissingReason.RATE_LIMITED
    with pytest.raises(ValueError):
        _missing(reason="network_glitch")
    with pytest.raises(ValueError):
        BuyersCompleteness(complete=False, reason="whatever", detail="")


# --- Transfer: сім полів доказу, без умовчань -----------------------------------


EVIDENCE = ("signature", "slot", "block_time", "sender", "receiver", "asset", "amount")


def test_transfer_requires_all_evidence_fields():
    fields = {f.name: f for f in dataclasses.fields(Transfer)}
    for name in EVIDENCE:
        assert name in fields
        assert fields[name].default is dataclasses.MISSING, name
        assert fields[name].default_factory is dataclasses.MISSING, name

    for name in EVIDENCE:
        kw = {k: v for k, v in TRANSFER_KW.items() if k != name}
        with pytest.raises(TypeError):
            Transfer(**kw)

    t = _transfer()
    assert (t.signature, t.slot, t.block_time, t.sender, t.receiver, t.asset, t.amount) == (
        SIG_A, 100, 1_700_000_000, W2, W1, "sol", 1_000_000,
    )


def test_transfer_has_no_defaults_at_all():
    for f in dataclasses.fields(Transfer):
        assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING, f.name


@pytest.mark.parametrize(
    "field,bad",
    [
        ("signature", ""),
        ("signature", None),
        ("sender", ""),
        ("receiver", None),
        ("slot", -1),
        ("slot", None),
        ("slot", True),
        ("slot", 1.0),
        ("block_time", -5),
        ("block_time", "1700000000"),
        ("amount", 0),
        ("amount", -1),
        ("amount", 1.5),
        ("amount", True),
        ("amount", "1000"),
        ("asset", "usdc"),
        ("asset", "spl:"),
        ("asset", None),
        ("instruction_path", ""),
        ("instruction_path", "1.2.3"),
        ("instruction_path", "a"),
        ("depth", 0),
        ("decimals", -1),
    ],
)
def test_transfer_rejects_invalid_evidence(field, bad):
    with pytest.raises((TypeError, ValueError)):
        _transfer(**{field: bad})


def test_transfer_block_time_may_be_none_but_must_be_passed():
    assert _transfer(block_time=None).block_time is None


def test_transfer_sol_has_no_decimals_spl_keeps_them():
    with pytest.raises(ValueError):
        _transfer(asset="sol", decimals=9)
    t = _transfer(asset="spl:" + MINT, decimals=9)
    assert t.asset == Asset.spl(MINT)
    assert t.asset.mint == MINT


def test_transfer_asset_string_is_normalised_to_asset():
    t = _transfer(asset="sol")
    assert isinstance(t.asset, Asset)
    assert t.asset == Asset.SOL == "sol"
    assert Asset.SOL.mint is None


def test_transfer_is_frozen():
    with pytest.raises(AttributeError):
        _transfer().amount = 1  # type: ignore[misc]


# --- Ключі порядку: лише з даних ------------------------------------------------


def test_sort_keys_are_data_only():
    b = _buyer()
    assert buyer_sort_key(b) == (b.first_buy_slot, b.first_buy_signature, b.wallet)
    # rank — наслідок упорядкування, не його причина.
    assert buyer_sort_key(dataclasses.replace(b, rank=99)) == buyer_sort_key(b)
    assert buyer_sort_key(dataclasses.replace(b, received_amount=1)) == buyer_sort_key(b)

    t = _transfer()
    assert transfer_sort_key(t)[:2] == (t.slot, t.signature)
    # depth і сума не впливають на позицію переказу.
    assert transfer_sort_key(dataclasses.replace(t, depth=3, amount=7)) == transfer_sort_key(t)

    # Рівні дані -> рівні ключі; результат не залежить від порядку на вході.
    buyers = [
        _buyer(W3, slot=100, sig=SIG_B),
        _buyer(W2, slot=100, sig=SIG_A),
        _buyer(W1, slot=100, sig=SIG_A),
        _buyer(W1, slot=99, sig=SIG_B),
    ]
    transfers = [
        _transfer(slot=5, signature=SIG_B, instruction_path="0"),
        _transfer(slot=5, signature=SIG_A, instruction_path="10"),
        _transfer(slot=5, signature=SIG_A, instruction_path="2"),
        _transfer(slot=5, signature=SIG_A, instruction_path="2.1"),
        _transfer(slot=4, signature=SIG_B, instruction_path="3"),
    ]
    expected_b = sorted(buyers, key=buyer_sort_key)
    expected_t = sorted(transfers, key=transfer_sort_key)
    rng = random.Random(1)
    for _ in range(20):
        rng.shuffle(buyers)
        rng.shuffle(transfers)
        assert sorted(buyers, key=buyer_sort_key) == expected_b
        assert sorted(transfers, key=transfer_sort_key) == expected_t

    assert [(b.first_buy_slot, b.first_buy_signature, b.wallet) for b in expected_b] == [
        (99, SIG_B, W1), (100, SIG_A, W1), (100, SIG_A, W2), (100, SIG_B, W3),
    ]
    # instruction_path упорядковується числово: "2" < "2.1" < "10".
    assert [(t.slot, t.signature, t.instruction_path) for t in expected_t] == [
        (4, SIG_B, "3"), (5, SIG_A, "2"), (5, SIG_A, "2.1"), (5, SIG_A, "10"), (5, SIG_B, "0"),
    ]


# --- Решта типів: frozen, узгодженість, відмова значенням ------------------------


def test_buyer_requires_nonempty_spent_and_positive_amount():
    with pytest.raises(ValueError):
        _buyer(spent=())
    with pytest.raises(ValueError):
        _buyer(received_amount=0)
    with pytest.raises(ValueError):
        _buyer(rank=0)
    with pytest.raises(ValueError):
        Spend(asset="sol", amount=0)


def test_buyer_lists_are_frozen_to_tuples():
    b = _buyer(spent=[Spend(asset="sol", amount=1)], programs=["P"])
    assert isinstance(b.spent, tuple) and isinstance(b.programs, tuple)
    assert b.address_type is AddressType.WALLET
    assert _buyer(address_type="off_curve").address_type is AddressType.OFF_CURVE


def test_unexpanded_node_reason_is_enum():
    n = UnexpandedNode(
        wallet=W1, depth=0, reason="high_degree", counterparties_seen=201,
        signatures_seen=300, signatures_truncated=False,
    )
    assert n.reason is UnexpandedReason.HIGH_DEGREE
    with pytest.raises(ValueError):
        dataclasses.replace(n, reason="too_busy")


def test_ingest_result_takes_completeness_only_from_derive():
    meta = _metadata(wallets_analyzed=1)
    r = IngestResult(
        metadata=meta,
        completeness=Completeness.derive([_missing()], _buyers_ok()),
        buyers=[_buyer()],
        transfers=[_transfer()],
        unexpanded=[],
    )
    assert r.completeness.status is CompletenessStatus.INCOMPLETE
    assert isinstance(r.buyers, tuple) and isinstance(r.transfers, tuple)

    # Імітація completeness (dict/рядок) не проходить.
    with pytest.raises(TypeError):
        IngestResult(metadata=meta, completeness={"status": "complete"}, buyers=[_buyer()],
                     transfers=[], unexpanded=[])
    with pytest.raises(TypeError):
        IngestResult(metadata=meta, completeness="complete", buyers=[_buyer()],
                     transfers=[], unexpanded=[])


def test_ingest_result_rejects_inconsistent_contents():
    ok = Completeness.derive([], _buyers_ok())
    # wallets_analyzed суперечить вибірці.
    with pytest.raises(ValueError):
        IngestResult(metadata=_metadata(2), completeness=ok, buyers=[_buyer()],
                     transfers=[], unexpanded=[])
    # Дубль покупця.
    with pytest.raises(ValueError):
        IngestResult(metadata=_metadata(2), completeness=ok,
                     buyers=[_buyer(rank=1), _buyer(rank=2)], transfers=[], unexpanded=[])
    # Дубль переказу за ключем (signature, instruction_path).
    with pytest.raises(ValueError):
        IngestResult(metadata=_metadata(0), completeness=ok, buyers=[],
                     transfers=[_transfer(), _transfer(depth=2)], unexpanded=[])


def test_rejection_is_a_value_and_cache_error_is_an_exception():
    r = Rejection(kind="token_not_found", mint=MINT, detail="account_missing")
    assert r.kind is RejectKind.TOKEN_NOT_FOUND
    with pytest.raises(ValueError):
        Rejection(kind="bad_luck", mint=MINT, detail="")
    assert issubclass(CacheInvariantError, Exception)
    assert not isinstance(r, BaseException)


def test_enum_values_match_contract():
    assert {e.value for e in CompletenessStatus} == {"complete", "incomplete"}
    assert {e.value for e in MissingReason} == {
        "rate_limited", "timeout", "unavailable", "corrupt_data", "budget_exhausted",
    }
    assert {e.value for e in UnexpandedReason} == {"high_degree", "signature_cap"}
    assert {e.value for e in RejectKind} == {"invalid_address", "token_not_found"}
    assert {e.value for e in AddressType} == {"wallet", "off_curve"}


def test_all_entities_are_frozen():
    for cls in (Spend, Buyer, Transfer, UnexpandedNode, MissingHistory, BuyersCompleteness,
                Completeness, RunMetadata, IngestResult, Rejection):
        assert cls.__dataclass_params__.frozen, cls.__name__
