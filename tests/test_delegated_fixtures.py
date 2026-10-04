# verifies: FR-002-15, FR-002-16
"""Фікстура `swapsend` генератора 001 (T-043): делегована купівля, неоднозначні й «не зв'язки».

`expected.json` сценарію складено генератором з ДЕКЛАРАЦІЇ (хто платник, хто отримувач, які
транзакції не дають зв'язку), а не з коду збору (його ще немає на цей момент). Тут доводиться,
що (1) додавання сценарію не зачепило жоден байт `basic/hub/corrupt/notfound` (SC-006), (2) записані
в `rpc.json` баланси справді мають форму, яку декларація стверджує, причому перевірено окремим
перебором балансів у самому тесті, а не кодом `unmask`, (3) сценарій читається фікстурним
джерелом так само, як інші.

Правило еталона (research R-2): платник P — Δ(mint)==0 і витратив (SOL понад комісію й ренту
створених ним рахунків, або від'ємна дельта іншого токена); отримувач R — Δ(mint)>0 і нічого
не витратив; 1:1 → зв'язок; обидві сторони непорожні й не 1:1 → кандидати без пари; інакше нічого.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from unmask.ingest.budget import FakeClock
from unmask.ingest.rpc.fixture import FixtureRpcSource

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = FIXTURES / "scenarios"
BUILDER = FIXTURES / "build_fixtures.py"
DEADLINE = None

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# sha256 файлів сценаріїв 001, знятих ДО додавання swapsend (SC-006: жоден байт не змінюється).
SCENARIOS_001_SHA256 = {
    "basic/expected.json": "ac0376590b705084874d70361a0a1fe94e19d6ac58ac8f6076b74c5b3aea6ac4",
    "basic/rpc.json": "d8fa428186bdbcf372aca10cdb95cba8b75b3e037253841ec28e484d38f69392",
    "corrupt/rpc.json": "0aa730594f7f2cee2b47bb9f5a6201bb16e07920ab5995f88c8274df01743e76",
    "hub/expected.json": "5b52c186d7ed11b1db9a964b655f86a402f0cac03b73b9d3c61a358f82ef8f5d",
    "hub/rpc.json": "2158d85dba9edb4ee240346fc78681df1f3512b0875fab92d10d6454ab1cb570",
    "notfound/rpc.json": "7fe9d099543db51b5dfce036f340d69747c36a6d5caf28f7c017b8ede85579aa",
}
SWAPSEND_FILES = {"swapsend/rpc.json", "swapsend/expected.json"}


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_fixtures_delegated", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _load_builder()


def _rpc(name="swapsend"):
    return json.loads((SCENARIOS / name / "rpc.json").read_text(encoding="utf-8"))


def _expected(name="swapsend"):
    return json.loads((SCENARIOS / name / "expected.json").read_text(encoding="utf-8"))


# --- незалежний перебір балансів (не код unmask) ---------------------------------------


def _instructions(tx):
    """Усі інструкції транзакції, верхнього рівня й внутрішні."""
    top = tx["transaction"]["message"]["instructions"]
    inner = [ix for group in tx["meta"]["innerInstructions"] for ix in group["instructions"]]
    return list(top) + inner


def _balances(tx, mint):
    """(ключі, Δ SOL за адресою, Δ токена mint за власником, від'ємні дельти інших токенів за власником)."""
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    meta = tx["meta"]
    sol = {k: post - pre for k, pre, post in zip(keys, meta["preBalances"], meta["postBalances"])}

    def by_owner(side):
        out = {}
        for b in meta[side]:
            key = (b["owner"], b["mint"])
            out[key] = out.get(key, 0) + int(b["uiTokenAmount"]["amount"])
        return out

    pre, post = by_owner("preTokenBalances"), by_owner("postTokenBalances")
    delta = {key: post.get(key, 0) - pre.get(key, 0) for key in set(pre) | set(post)}
    mint_delta = {owner: d for (owner, m), d in delta.items() if m == mint}
    other_negative = {owner for (owner, m), d in delta.items() if m != mint and d < 0}
    return keys, sol, mint_delta, other_negative


def _created_lamports(tx, owner):
    return sum(
        ix["parsed"]["info"]["lamports"]
        for ix in _instructions(tx)
        if ix.get("parsed", {}).get("type") == "createAccount" and ix["parsed"]["info"]["source"] == owner
    )


def _payers_and_receivers(tx, mint):
    """P і R за правилом research R-2, обчислені лише з балансів `meta` і `createAccount`."""
    if tx["meta"]["err"] is not None:
        return [], []
    keys, sol, mint_delta, other_negative = _balances(tx, mint)
    fee_payer = keys[0]
    owners = set(keys) | set(mint_delta)

    def spent(owner):
        lamports = -sol.get(owner, 0)
        if owner == fee_payer:
            lamports -= tx["meta"]["fee"]
        lamports -= _created_lamports(tx, owner)
        return lamports > 0 or owner in other_negative

    payers = sorted(o for o in owners if mint_delta.get(o, 0) == 0 and spent(o))
    receivers = sorted(o for o in owners if mint_delta.get(o, 0) > 0 and not spent(o))
    return payers, receivers


def _oracle(rpc, mint):
    """Зв'язки й кандидати з балансів `rpc.json` — для звірки з декларацією генератора."""
    links, unpaired = [], []
    for sig, tx in rpc["getTransaction"].items():
        payers, receivers = _payers_and_receivers(tx, mint)
        base = {"signature": sig, "slot": tx["slot"], "block_time": tx["blockTime"]}
        if len(payers) == 1 and len(receivers) == 1:
            links.append(dict(base, payer=payers[0], receiver=receivers[0]))
        elif payers and receivers:
            detail = f"payers={len(payers)} receivers={len(receivers)}"
            unpaired += [dict(base, wallet=w, side="payer", detail=detail) for w in payers]
            unpaired += [dict(base, wallet=w, side="receiver", detail=detail) for w in receivers]
    links.sort(key=lambda r: (r["slot"], r["signature"], r["payer"], r["receiver"]))
    unpaired.sort(key=lambda r: (r["slot"], r["signature"], r["wallet"], r["side"]))
    return links, unpaired


# --- (1) детермінізм і незмінність наявних сценаріїв -----------------------------------


def test_build_is_deterministic_and_existing_scenarios_unchanged(builder):
    # `build_all` — незмінний контракт 001: рівно чотири сценарії, байти збігаються з комітом
    base = builder.build_all()
    assert set(base) == set(SCENARIOS_001_SHA256)
    for rel, text in base.items():
        committed = (SCENARIOS / rel).read_text(encoding="utf-8")
        assert committed == text, f"{rel}: вихід генератора відрізняється від закоміченого файла"
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == SCENARIOS_001_SHA256[rel], (
            f"{rel}: змінено сценарій 001 (SC-006)")
    # розширена збірка = 001 + swapsend; дві збірки поспіль побайтно однакові
    first, second = builder.build_extended(), builder.build_extended()
    assert first == second
    assert set(first) == set(SCENARIOS_001_SHA256) | SWAPSEND_FILES
    for rel, text in base.items():
        assert first[rel] == text  # додавання сценарію не зачіпає інші
    for rel in SWAPSEND_FILES:
        assert (SCENARIOS / rel).read_text(encoding="utf-8") == first[rel], f"{rel}: перегенеруйте"
    assert {p.name for p in (SCENARIOS / "swapsend").iterdir()} == {"rpc.json", "expected.json"}


# --- (2) форма записаних балансів ------------------------------------------------------


def test_swapsend_delegated_tx_has_payer_without_mint_delta_and_receiver_without_spend():
    rpc, exp = _rpc(), _expected()
    mint = exp["mint"]
    [link] = exp["delegated"]["links"]
    tx = rpc["getTransaction"][link["signature"]]
    payer, receiver = exp["wallets"]["A"], exp["wallets"]["B"]
    assert (link["payer"], link["receiver"]) == (payer, receiver)
    keys, sol, mint_delta, other_negative = _balances(tx, mint)
    # платник: fee payer і підписант, SOL пішов понад комісію й ренту, токена mint не отримав
    assert keys[0] == payer and tx["transaction"]["message"]["accountKeys"][0]["signer"] is True
    assert mint_delta.get(payer, 0) == 0
    assert -sol[payer] - tx["meta"]["fee"] - _created_lamports(tx, payer) > 0
    assert all(b["owner"] != payer for b in tx["meta"]["postTokenBalances"])  # у нього немає токен-рахунку взагалі
    # отримувач: отримав токен, SOL не рухався, нічого не списано в інших токенах
    assert mint_delta[receiver] > 0
    assert sol[receiver] == 0 and receiver not in other_negative
    assert receiver in keys and not tx["transaction"]["message"]["accountKeys"][keys.index(receiver)]["signer"]
    # ATA отримувача створив і оплатив платник; inner-переказ: пул -> ATA(отримувач)
    creates = [ix for ix in _instructions(tx) if ix.get("parsed", {}).get("type") == "create"]
    [create] = creates
    assert create["parsed"]["info"]["source"] == payer and create["parsed"]["info"]["wallet"] == receiver
    ata = create["parsed"]["info"]["account"]
    transfers = [ix["parsed"]["info"] for ix in _instructions(tx)
                 if ix.get("parsed", {}).get("type") in ("transfer", "transferChecked") and ix["program"] == "spl-token"]
    assert [(t["source"], t["destination"]) for t in transfers] == [(exp["wallets"]["POOL_ATA"], ata)]
    # платник не покупець: жодної купівлі за правилом SOL+токен
    assert payer not in {b["wallet"] for b in exp["buyers"]}
    assert receiver not in {b["wallet"] for b in exp["buyers"]}


def test_swapsend_multi_transactions_have_declared_payers_and_receivers():
    rpc, exp = _rpc(), _expected()
    mint = exp["mint"]
    shapes = {}
    for cand in exp["delegated"]["unpaired"]:
        shapes.setdefault(cand["signature"], {"payer": set(), "receiver": set(), "detail": cand["detail"]})
        shapes[cand["signature"]][cand["side"]].add(cand["wallet"])
    assert sorted((len(s["payer"]), len(s["receiver"])) for s in shapes.values()) == [(1, 2), (2, 2)]
    for sig, shape in shapes.items():
        tx = rpc["getTransaction"][sig]
        signers = {k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"] if k["signer"]}
        assert signers == shape["payer"], "кожен платник — підписант транзакції"
        keys, sol, mint_delta, _ = _balances(tx, mint)
        for payer in shape["payer"]:
            assert mint_delta.get(payer, 0) == 0
            assert sol[payer] < 0
        for receiver in shape["receiver"]:
            assert mint_delta[receiver] > 0 and sol[receiver] == 0
        assert shape["detail"] == f"payers={len(shape['payer'])} receivers={len(shape['receiver'])}"
        assert not tx["meta"]["err"]
    # усі учасники кандидатів не є покупцями
    buyers = {b["wallet"] for b in exp["buyers"]}
    assert not buyers & {c["wallet"] for c in exp["delegated"]["unpaired"]}


# --- (3) декларація expected.json ------------------------------------------------------


def test_swapsend_expected_lists_exactly_one_link_and_declared_unpaired():
    rpc, exp = _rpc(), _expected()
    delegated = exp["delegated"]
    assert set(delegated) >= {"links", "unpaired"}
    w = exp["wallets"]
    # рівно один зв'язок: A -> B
    [link] = delegated["links"]
    assert set(link) == {"signature", "slot", "block_time", "payer", "receiver"}
    assert (link["payer"], link["receiver"]) == (w["A"], w["B"])
    assert link["signature"] in rpc["getTransaction"]
    assert link["slot"] == rpc["getTransaction"][link["signature"]]["slot"]
    # кандидати: 2 платники × 2 отримувачі -> 4, 1 платник × 2 отримувачі -> 3
    unpaired = delegated["unpaired"]
    assert len(unpaired) == 7
    assert {frozenset(c) for c in unpaired} == {frozenset(["signature", "slot", "block_time", "wallet", "side", "detail"])}
    by_sig = {}
    for cand in unpaired:
        by_sig.setdefault(cand["signature"], []).append(cand)
    assert sorted(len(v) for v in by_sig.values()) == [3, 4]
    four = next(v for v in by_sig.values() if len(v) == 4)
    three = next(v for v in by_sig.values() if len(v) == 3)
    assert {c["detail"] for c in four} == {"payers=2 receivers=2"}
    assert {c["detail"] for c in three} == {"payers=1 receivers=2"}
    assert {(c["wallet"], c["side"]) for c in four} == {
        (w["C1"], "payer"), (w["C2"], "payer"), (w["D1"], "receiver"), (w["D2"], "receiver")}
    assert {(c["wallet"], c["side"]) for c in three} == {
        (w["E"], "payer"), (w["F1"], "receiver"), (w["F2"], "receiver")}
    # контрактний порядок: links (slot, signature, payer, receiver), unpaired (slot, signature, wallet, side)
    assert unpaired == sorted(unpaired, key=lambda c: (c["slot"], c["signature"], c["wallet"], c["side"]))
    # декларація збігається з незалежним перебором балансів rpc.json
    links, oracle_unpaired = _oracle(rpc, exp["mint"])
    assert links == delegated["links"]
    assert oracle_unpaired == unpaired
    # зв'язок і кандидати — усередині вікна перших N покупців (до слота останнього з них)
    window_end = max(b["first_buy_slot"] for b in exp["buyers"])
    assert exp["config"]["first_buyers_n"] == len(exp["buyers"]) == 3
    assert all(r["slot"] <= window_end for r in delegated["links"] + unpaired)


def test_swapsend_airdrop_and_mint_declared_as_no_link():
    rpc, exp = _rpc(), _expected()
    mint = exp["mint"]
    no_link = {entry["reason"]: entry for entry in exp["delegated"]["no_link"]
               if entry["reason"] in ("airdrop_creator_pays_rent", "mint_to_creation")}
    assert set(no_link) == {"airdrop_creator_pays_rent", "mint_to_creation"}
    declared_sigs = {e["signature"] for e in exp["delegated"]["no_link"]}
    emitted = {r["signature"] for r in exp["delegated"]["links"] + exp["delegated"]["unpaired"]}
    assert declared_sigs <= set(rpc["getTransaction"]) and not declared_sigs & emitted
    buyers = {b["first_buy_signature"] for b in exp["buyers"]}
    # звичайні купівлі декларовані як «не зв'язок» окремою причиною
    assert {e["signature"] for e in exp["delegated"]["no_link"] if e["reason"] == "ordinary_purchase"} == buyers

    airdrop = rpc["getTransaction"][no_link["airdrop_creator_pays_rent"]["signature"]]
    keys, sol, mint_delta, _ = _balances(airdrop, mint)
    creator = exp["wallets"]["CREATOR"]
    recipient = exp["wallets"]["AIR"]
    assert mint_delta[recipient] > 0 and sol[recipient] == 0  # отримувач нічого не витратив
    assert mint_delta[creator] < 0  # творець віддав токен: не «платник без токена»
    assert -sol[creator] == airdrop["meta"]["fee"] + _created_lamports(airdrop, creator)  # лише комісія й рента
    assert _payers_and_receivers(airdrop, mint) == ([], [recipient])

    creation = rpc["getTransaction"][no_link["mint_to_creation"]["signature"]]
    assert any(ix.get("parsed", {}).get("type") == "mintTo" for ix in _instructions(creation))
    keys, sol, mint_delta, _ = _balances(creation, mint)
    assert mint_delta[creator] > 0 and mint_delta[exp["wallets"]["POOL"]] > 0
    assert -sol[creator] == creation["meta"]["fee"] + _created_lamports(creation, creator)
    assert _payers_and_receivers(creation, mint)[0] == []  # платників немає


# --- (4) фікстурне джерело -------------------------------------------------------------


def test_swapsend_loads_in_fixture_source():
    exp = _expected()
    mint = exp["mint"]
    src = FixtureRpcSource(SCENARIOS / "swapsend", clock=FakeClock())
    assert src.name == "fixture:swapsend"
    info = src.get_account_info(mint, deadline=DEADLINE)
    assert info["owner"] == TOKEN and info["data"]["parsed"]["type"] == "mint"
    sigs = src.get_signatures_for_address(mint, before=None, until=None, limit=1000, deadline=DEADLINE)
    paged, cursor = [], None
    while True:
        page = src.get_signatures_for_address(mint, before=cursor, until=None, limit=3, deadline=DEADLINE)
        if not page:
            break
        paged += page
        cursor = page[-1]["signature"]
    assert paged == sigs
    txs = src.get_transactions([s["signature"] for s in sigs], deadline=DEADLINE)
    assert all(tx is not None and tx["meta"]["err"] == s["err"] for tx, s in zip(txs, sigs))
    # усі підписи, названі в декларації, є в історії mint
    named = {e["signature"] for e in exp["delegated"]["no_link"]}
    named |= {r["signature"] for r in exp["delegated"]["links"] + exp["delegated"]["unpaired"]}
    assert named <= {s["signature"] for s in sigs}
    # покупці — рівно P1..P3 за (слот, підпис, гаманець); B та інші отримувачі серед них немає
    assert [b["wallet"] for b in exp["buyers"]] == [exp["wallets"][f"P{i}"] for i in (1, 2, 3)]
    assert [b["rank"] for b in exp["buyers"]] == [1, 2, 3]
    for buyer in exp["buyers"]:
        accounts = src.get_token_accounts_by_owner(buyer["wallet"], deadline=DEADLINE)
        assert any(a["account"]["data"]["parsed"]["info"]["mint"] == mint for a in accounts)
    receiver = exp["delegated"]["links"][0]["receiver"]
    accounts = src.get_token_accounts_by_owner(receiver, deadline=DEADLINE)
    assert any(a["account"]["data"]["parsed"]["info"]["mint"] == mint for a in accounts)
    assert receiver not in {b["wallet"] for b in exp["buyers"]}
