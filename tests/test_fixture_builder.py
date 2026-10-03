# verifies: FR-001-15
"""Генератор синтетичних сценаріїв: детермінізм, форма JSON-RPC, узгодженість rpc.json з expected.json.

`expected.json` будується генератором декларативно (явні таблиці переказів із глибиною й
причинами виключення), а не обходом `rpc.json`; тести тут лише доводять, що еталон і записані
відповіді не суперечать одне одному і що «пастки» (переказ після купівлі, невдала транзакція,
самопереказ, причинне відсікання, межа глибини) справді присутні у даних.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from unmask.ingest.budget import FakeClock
from unmask.ingest.rpc.fixture import FixtureRpcSource

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIOS = FIXTURES / "scenarios"
BUILDER = FIXTURES / "build_fixtures.py"

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_PROGRAMS = {TOKEN, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
ALL = ["basic", "hub", "corrupt", "notfound"]
WITH_TX = ["basic", "hub", "corrupt"]
WITH_EXPECTED = ["basic", "hub"]
DEADLINE = None


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_fixtures", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # FileNotFoundError, якщо генератора немає
    return module


@pytest.fixture(scope="module")
def builder():
    return _load_builder()


def _rpc(name):
    return json.loads((SCENARIOS / name / "rpc.json").read_text(encoding="utf-8"))


def _expected(name):
    return json.loads((SCENARIOS / name / "expected.json").read_text(encoding="utf-8"))


def _tx_signatures(rpc):
    return set(rpc.get("getTransaction", {}))


def _cases(name):
    """Для basic — один «випадок» (сам файл); для hub — кожен елемент `cases`."""
    exp = _expected(name)
    if "cases" in exp:
        return [(case["name"], case) for case in exp["cases"]]
    return [(name, exp)]


# --- детермінізм і збіг із закоміченими файлами ------------------------------------


def test_build_is_deterministic_and_matches_committed_files(builder):
    first = builder.build_all()
    second = builder.build_all()
    assert first == second  # дві збірки поспіль — побайтно однакові
    expected_paths = {f"{n}/rpc.json" for n in ALL} | {f"{n}/expected.json" for n in WITH_EXPECTED}
    assert set(first) == expected_paths
    for rel, text in first.items():
        committed = (SCENARIOS / rel).read_text(encoding="utf-8")
        assert committed == text, f"{rel} відрізняється від виходу генератора; перегенеруйте"
    # у каталогах згенерованих сценаріїв немає сторонніх файлів
    for name in ALL:
        files = {p.name for p in (SCENARIOS / name).iterdir()}
        assert files == {Path(r).name for r in first if r.startswith(f"{name}/")}


# --- цілісність записаних відповідей -------------------------------------------------


@pytest.mark.parametrize("name", WITH_TX)
def test_every_listed_signature_has_a_transaction_or_explicit_null(name):
    rpc = _rpc(name)
    known = _tx_signatures(rpc)
    listed = {
        e["signature"] for history in rpc["getSignaturesForAddress"].values() for e in history
    }
    assert listed, "сценарій без жодного підпису"
    assert listed <= known, f"підписи без запису getTransaction (навіть null): {sorted(listed - known)[:3]}"
    # і навпаки: у getTransaction немає підписів-сиріт, що не фігурують в жодній історії
    assert known <= listed


@pytest.mark.parametrize("name", WITH_TX)
def test_signature_histories_are_newest_first_and_consistent_with_transactions(name):
    rpc = _rpc(name)
    for address, history in rpc["getSignaturesForAddress"].items():
        slots = [e["slot"] for e in history]
        assert slots == sorted(slots, reverse=True), f"{address}: історія не від найновішого"
        assert len({e["signature"] for e in history}) == len(history)
        for entry in history:
            assert set(entry) == {"signature", "slot", "err", "memo", "blockTime", "confirmationStatus"}
            tx = rpc["getTransaction"][entry["signature"]]
            if tx is None:
                continue
            assert tx["slot"] == entry["slot"] and tx["blockTime"] == entry["blockTime"]
            if "meta" in tx:
                assert tx["meta"]["err"] == entry["err"]
            keys = {k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]}
            assert address in keys, "адреса в історії, але не в accountKeys транзакції"


def _defective(rpc):
    meta = rpc.get("_meta", {}).get("defects", {})
    return set(meta.get("missing_meta", []))


@pytest.mark.parametrize("name", WITH_TX)
def test_transactions_have_jsonrpc_shape(name):
    rpc = _rpc(name)
    skip = _defective(rpc)
    checked = 0
    for sig, tx in rpc["getTransaction"].items():
        if tx is None or sig in skip:
            continue
        checked += 1
        for key in ("slot", "blockTime", "transaction", "meta"):
            assert key in tx, f"{sig}: немає {key}"
        message = tx["transaction"]["message"]
        assert tx["transaction"]["signatures"][0] == sig
        keys = message["accountKeys"]
        assert keys and all({"pubkey", "signer", "writable", "source"} <= set(k) for k in keys)
        assert keys[0]["signer"] and keys[0]["writable"]  # платник комісії
        meta = tx["meta"]
        for key in (
            "err", "fee", "preBalances", "postBalances",
            "preTokenBalances", "postTokenBalances", "innerInstructions",
        ):
            assert key in meta, f"{sig}: meta без {key}"
        n = len(keys)
        assert len(meta["preBalances"]) == n and len(meta["postBalances"]) == n
        # лампорти зберігаються: усе, що пішло, — це комісія (також для невдалих транзакцій)
        assert sum(meta["preBalances"]) - meta["fee"] == sum(meta["postBalances"]), sig
        for side in ("preTokenBalances", "postTokenBalances"):
            for tb in meta[side]:
                assert 0 <= tb["accountIndex"] < n
                assert {"mint", "owner", "programId", "uiTokenAmount"} <= set(tb)
                assert {"amount", "decimals", "uiAmount", "uiAmountString"} <= set(tb["uiTokenAmount"])
        top = len(message["instructions"])
        for group in meta["innerInstructions"]:
            assert 0 <= group["index"] < top and group["instructions"]
        for ix in message["instructions"]:
            assert "programId" in ix and ("parsed" in ix or ("accounts" in ix and "data" in ix))
    assert checked > 0


def test_corrupt_scenario_has_exactly_the_declared_defects():
    rpc = _rpc("corrupt")
    nulls = [s for s, tx in rpc["getTransaction"].items() if tx is None]
    no_meta = [s for s, tx in rpc["getTransaction"].items() if tx is not None and "meta" not in tx]
    assert len(nulls) == 1 and len(no_meta) == 1
    defects = rpc["_meta"]["defects"]
    assert defects["null_transaction"] == nulls
    assert defects["missing_meta"] == no_meta
    # обидві пошкоджені транзакції присутні в історії гаманця (інакше їх ніхто не запитає)
    listed = {e["signature"] for h in rpc["getSignaturesForAddress"].values() for e in h}
    assert set(nulls + no_meta) <= listed
    # решта сценарію здорова: є покупці й принаймні один нормальний переказ
    assert sum(1 for tx in rpc["getTransaction"].values() if tx and "meta" in tx) >= 4


def test_notfound_scenario_covers_missing_system_owned_and_non_mint_accounts():
    rpc = _rpc("notfound")
    accounts = rpc["getAccountInfo"]
    cast = rpc["_meta"]["cast"]
    assert accounts[cast["MISSING_MINT"]] is None
    wallet = accounts[cast["SYSTEM_OWNED"]]
    assert wallet["owner"] == SYSTEM and wallet["executable"] is False
    token_account = accounts[cast["TOKEN_ACCOUNT"]]
    assert token_account["owner"] in TOKEN_PROGRAMS
    assert token_account["data"]["parsed"]["type"] == "account"  # не mint, хоч і належить Token
    assert not any(a["data"]["parsed"]["type"] == "mint" for a in accounts.values() if a and isinstance(a["data"], dict))
    assert rpc["getSignaturesForAddress"] == {} and rpc["getTransaction"] == {}


# --- завантаження у фікстурне джерело -------------------------------------------------


def test_basic_scenario_loads_in_fixture_source():
    exp = _expected("basic")
    mint = exp["mint"]
    src = FixtureRpcSource(SCENARIOS / "basic", clock=FakeClock())
    assert src.name == "fixture:basic"
    info = src.get_account_info(mint, deadline=DEADLINE)
    assert info["owner"] == TOKEN and info["data"]["parsed"]["type"] == "mint"
    assert info["data"]["parsed"]["info"]["decimals"] == 6
    sigs = src.get_signatures_for_address(mint, before=None, until=None, limit=1000, deadline=DEADLINE)
    assert len(sigs) > len(exp["buyers"])  # окрім купівель є створення, ліквідність, ейрдроп
    # перегортання сторінками по 3 збирає ту саму історію
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
    for buyer in exp["buyers"]:
        owner = buyer["wallet"]
        accounts = src.get_token_accounts_by_owner(owner, deadline=DEADLINE)
        assert any(a["account"]["data"]["parsed"]["info"]["mint"] == mint for a in accounts), owner


def test_hub_scenario_loads_and_pages_through_the_busy_wallet():
    exp = _expected("hub")
    hub = exp["wallets"]["H"]
    src = FixtureRpcSource(SCENARIOS / "hub")
    first = src.get_signatures_for_address(hub, before=None, until=None, limit=1000, deadline=DEADLINE)
    rest = src.get_signatures_for_address(
        hub, before=first[-1]["signature"], until=None, limit=1000, deadline=DEADLINE
    )
    assert len(first) == 1000 and len(first) + len(rest) == 1200


# --- структура й узгодженість expected.json -------------------------------------------


def _resolve_instruction(tx, path):
    message = tx["transaction"]["message"]
    top, _, inner = path.partition(".")
    if not inner:
        return message["instructions"][int(top)]
    groups = {g["index"]: g["instructions"] for g in tx["meta"]["innerInstructions"]}
    return groups[int(top)][int(inner)]


def _token_owner(tx, token_account):
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    idx = keys.index(token_account)
    for side in ("postTokenBalances", "preTokenBalances"):
        for tb in tx["meta"][side]:
            if tb["accountIndex"] == idx:
                return tb["owner"]
    raise AssertionError(f"токен-рахунок {token_account} не описаний у token balances")


@pytest.mark.parametrize("name", WITH_EXPECTED)
def test_expected_transfers_are_backed_by_instructions_in_rpc(name):
    rpc = _rpc(name)
    for case_name, case in _cases(name):
        keys = set()
        for t in case["transfers"]:
            key = (t["signature"], t["instruction_path"])
            assert key not in keys, f"{case_name}: дубль {key}"
            keys.add(key)
            tx = rpc["getTransaction"][t["signature"]]
            assert tx["meta"]["err"] is None
            assert (tx["slot"], tx["blockTime"]) == (t["slot"], t["block_time"])
            ix = _resolve_instruction(tx, t["instruction_path"])
            assert t["sender"] != t["receiver"]
            assert 1 <= t["depth"] <= 3
            if t["asset"] == "sol":
                info = ix["parsed"]["info"]
                assert ix["parsed"]["type"] in ("transfer", "createAccount")
                assert (info["source"], info["destination"], info["lamports"]) == (
                    t["sender"], t["receiver"], t["amount"])
                assert t["decimals"] is None
            else:
                parsed = ix["parsed"]
                assert ix["programId"] in TOKEN_PROGRAMS
                info = parsed["info"]
                assert parsed["type"] in ("transfer", "transferChecked")
                amount = info["tokenAmount"]["amount"] if "tokenAmount" in info else info["amount"]
                assert int(amount) == t["amount"]
                assert _token_owner(tx, info["source"]) == t["sender"]
                assert _token_owner(tx, info["destination"]) == t["receiver"]
                assert t["asset"] == "spl:" + next(
                    tb["mint"] for tb in tx["meta"]["postTokenBalances"] if tb["owner"] == t["receiver"])
                assert t["decimals"] == 6
        order = [(t["slot"], t["signature"], t["instruction_path"]) for t in case["transfers"]]
        assert order == sorted(order), f"{case_name}: переказі не впорядковані (slot, signature, path)"


@pytest.mark.parametrize("name", WITH_EXPECTED)
def test_expected_buyers_are_ranked_and_backed_by_mint_history(name):
    rpc = _rpc(name)
    exp = _expected(name)
    mint = exp["mint"]
    history = {e["signature"]: e for e in rpc["getSignaturesForAddress"][mint]}
    buyers = exp["buyers"]
    assert [b["rank"] for b in buyers] == list(range(1, len(buyers) + 1))
    keys = [(b["first_buy_slot"], b["first_buy_signature"], b["wallet"]) for b in buyers]
    assert keys == sorted(keys)
    assert len({b["wallet"] for b in buyers}) == len(buyers)
    for b in buyers:
        entry = history[b["first_buy_signature"]]  # купівля є в історії mint
        assert entry["slot"] == b["first_buy_slot"] and entry["blockTime"] == b["first_buy_time"]
        tx = rpc["getTransaction"][b["first_buy_signature"]]
        assert tx["meta"]["err"] is None
        received = sum(
            int(tb["uiTokenAmount"]["amount"]) for tb in tx["meta"]["postTokenBalances"]
            if tb["owner"] == b["wallet"] and tb["mint"] == mint
        ) - sum(
            int(tb["uiTokenAmount"]["amount"]) for tb in tx["meta"]["preTokenBalances"]
            if tb["owner"] == b["wallet"] and tb["mint"] == mint
        )
        assert received == b["received_amount"] > 0
        assert b["spent"] and all(s["amount"] > 0 for s in b["spent"])
        programs = [ix["programId"] for ix in tx["transaction"]["message"]["instructions"]]
        assert programs == b["programs"]
        on_curve = Pubkey.from_string(b["wallet"]).is_on_curve()
        assert b["address_type"] == ("wallet" if on_curve else "off_curve")


@pytest.mark.parametrize("name", WITH_EXPECTED)
def test_excluded_traps_are_present_in_rpc_but_absent_from_expected(name):
    rpc = _rpc(name)
    for case_name, case in _cases(name):
        excluded = case["excluded"]
        assert excluded, f"{case_name}: порожній список пасток"
        in_expected = {t["signature"] for t in case["transfers"]}
        for item in excluded:
            assert item["signature"] in rpc["getTransaction"], f"{case_name}: {item}"
            assert item["signature"] not in in_expected, f"{case_name}: {item['reason']} потрапив у перекази"
            assert item["reason"]


def test_basic_expected_contains_every_required_situation():
    rpc = _rpc("basic")
    exp = _expected("basic")
    w = exp["wallets"]
    reasons = {e["reason"] for e in exp["excluded"]}
    assert {"after_first_buy", "failed_transaction", "self_transfer",
            "causal_cutoff", "beyond_funding_depth"} <= reasons
    depths = {t["depth"] for t in exp["transfers"]}
    assert depths == {1, 2, 3}  # глибина 4 (K->C) у еталон не входить
    assert exp["unexpanded"] == []
    assert exp["completeness"]["status"] == "complete"
    # P2 і P3 купують в одному слоті, порядок визначає підпис, а не ім'я й не порядок у списку
    by_wallet = {b["wallet"]: b for b in exp["buyers"]}
    p2, p3 = by_wallet[w["P2"]], by_wallet[w["P3"]]
    assert p2["first_buy_slot"] == p3["first_buy_slot"]
    assert p3["first_buy_signature"] < p2["first_buy_signature"] and p3["rank"] < p2["rank"]
    # P5 — off-curve власник; решта — звичайні гаманці
    assert by_wallet[w["P5"]]["address_type"] == "off_curve"
    assert {by_wallet[w[n]]["address_type"] for n in ("P1", "P2", "P3", "P4")} == {"wallet"}
    assert [b["rank"] for b in exp["buyers"] if b["wallet"] == w["P5"]] == [5]
    # P4 і P5 без вхідних переказів
    receivers = {t["receiver"] for t in exp["transfers"]}
    assert w["P4"] not in receivers and w["P5"] not in receivers
    # два однакові перекази в одній транзакції — різні instruction_path
    pairs = {}
    for t in exp["transfers"]:
        pairs.setdefault((t["signature"], t["sender"], t["receiver"], t["amount"]), set()).add(t["instruction_path"])
    assert any(len(paths) == 2 for paths in pairs.values())
    # перекази через CPI мають шлях виду "i.j"
    assert any("." in t["instruction_path"] for t in exp["transfers"])
    # цикл A<->D: обидва напрями присутні з різною глибиною
    edges = {(t["sender"], t["receiver"]): t["depth"] for t in exp["transfers"]}
    assert edges[(w["A"], w["D"])] == 3 and edges[(w["D"], w["A"])] == 2
    # причинне відсікання: A фінансується з G до останнього свого ребра (A->P2), але не з F після нього
    assert (w["G"], w["A"]) in edges and (w["F"], w["A"]) not in edges
    assert (w["E"], w["B"]) not in edges and (w["C"], w["B"]) in edges
    # SPL: є і переказ лише в історії токен-рахунку, і переказ, видимий в обох історіях
    spl = [t for t in exp["transfers"] if t["asset"].startswith("spl:")]
    assert len(spl) == 2 and {t["receiver"] for t in spl} == {w["P2"]}
    wallet_history = {e["signature"] for e in rpc["getSignaturesForAddress"].get(w["P2"], [])}
    in_wallet = [t["signature"] in wallet_history for t in spl]
    assert sorted(in_wallet) == [False, True]
    for t in spl:
        accounts = {a["pubkey"] for a in rpc["getTokenAccountsByOwner"][w["P2"]]}
        token_histories = [
            {e["signature"] for e in rpc["getSignaturesForAddress"].get(acct, [])} for acct in accounts
        ]
        assert any(t["signature"] in h for h in token_histories)


def test_basic_wallets_are_valid_pubkeys_and_pool_is_a_pda():
    exp = _expected("basic")
    for label, address in exp["wallets"].items():
        key = Pubkey.from_string(address)
        assert key.is_on_curve() == (label != "P5"), label


def test_hub_scenario_matches_its_description():
    rpc = _rpc("hub")
    exp = _expected("hub")
    hub = exp["wallets"]["H"]
    history = rpc["getSignaturesForAddress"][hub]
    assert len(history) == 1200
    senders, inbound = set(), []
    for sig, tx in rpc["getTransaction"].items():
        if tx["meta"]["err"] is not None:
            continue
        for ix in tx["transaction"]["message"]["instructions"]:
            parsed = ix.get("parsed")
            if parsed and parsed["type"] == "transfer" and parsed["info"].get("destination") == hub:
                senders.add(parsed["info"]["source"])
                inbound.append(sig)
    assert len(senders) == 6  # шість унікальних відправників у вхідних переказах
    assert len(inbound) > 20
    # хаб фінансує покупця
    assert any(t["sender"] == hub for _, c in _cases("hub") for t in c["transfers"])


def test_hub_cases_cover_degree_cap_and_control():
    cases = dict(_cases("hub"))
    assert set(cases) == {"hub_high_degree", "hub_signature_cap", "hub_control"}
    degree, cap, control = cases["hub_high_degree"], cases["hub_signature_cap"], cases["hub_control"]
    for case in cases.values():
        assert case["config"]["first_buyers_n"] == 2
        assert case["completeness"]["status"] == "complete"  # обмеження розгортання не є неповнотою
    assert [u["reason"] for u in degree["unexpanded"]] == ["high_degree"]
    assert [u["reason"] for u in cap["unexpanded"]] == ["signature_cap"]
    assert cap["unexpanded"][0]["signatures_seen"] == cap["config"]["max_signatures_per_wallet"]
    assert cap["unexpanded"][0]["signatures_truncated"] is True
    assert control["unexpanded"] == []
    # контроль без обмежень збирає строго більше, ніж випадок з відсіканням за зв'язністю
    assert len(control["transfers"]) > len(degree["transfers"]) > 2
    hub = _expected("hub")["wallets"]["H"]
    assert degree["unexpanded"][0]["wallet"] == hub and degree["unexpanded"][0]["depth"] == 1


# --- перехресна перевірка еталона незалежним «грубим» оракулом ----------------------------
# Оракул читає лише rpc.json (як це зробить збір) і реалізує правила research.md у найпростішому
# вигляді. Збіг із декларативним expected.json ловить помилки в самому еталоні (пропущений
# переказ, хибна глибина, купівля, якої немає за правилом R-2).


def _chronology(rpc):
    """Порядок запису в getTransaction = порядок створення = хронологічний (для транзакцій з даними)."""
    return {sig: i for i, sig in enumerate(rpc["getTransaction"])}


def _oracle_purchases(rpc, mint):
    """Правило R-2 за балансовими дельтами; повертає {wallet: (slot, sig, received, spent_sol_or_spl)}."""
    entries = list(reversed(rpc["getSignaturesForAddress"][mint]))  # від найстарішого
    first = {}
    for entry in entries:
        tx = rpc["getTransaction"][entry["signature"]]
        if tx is None or tx["meta"]["err"] is not None:
            continue
        keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
        meta = tx["meta"]
        delta_token = {}
        for side, sign in (("postTokenBalances", 1), ("preTokenBalances", -1)):
            for tb in meta[side]:
                k = (tb["owner"], tb["mint"])
                delta_token[k] = delta_token.get(k, 0) + sign * int(tb["uiTokenAmount"]["amount"])
        created = sum(
            post for pre, post in zip(meta["preBalances"], meta["postBalances"]) if pre == 0)
        for (owner, m), delta in sorted(delta_token.items()):
            if m != mint or delta <= 0:
                continue
            i = keys.index(owner)
            spent_sol = meta["preBalances"][i] - meta["postBalances"][i]
            if i == 0:
                spent_sol -= meta["fee"] + created
            spent = [{"asset": "sol", "amount": spent_sol}] if spent_sol > 0 else []
            spent += [{"asset": f"spl:{m2}", "amount": -d2} for (o2, m2), d2 in sorted(delta_token.items())
                      if o2 == owner and m2 != mint and d2 < 0]
            if spent and owner not in first:
                first[owner] = {"slot": entry["slot"], "signature": entry["signature"],
                                "received_amount": delta, "spent": spent}
    return first


@pytest.mark.parametrize("name", WITH_EXPECTED)
def test_expected_buyers_match_independent_purchase_rule(name):
    rpc, exp = _rpc(name), _expected(name)
    first = _oracle_purchases(rpc, exp["mint"])
    ranked = sorted(first, key=lambda o: (first[o]["slot"], first[o]["signature"], o))
    n = len(exp["buyers"])
    got = [
        {"wallet": o, "slot": first[o]["slot"], "signature": first[o]["signature"],
         "received": first[o]["received_amount"], "spent": first[o]["spent"]}
        for o in ranked[:n]
    ]
    want = [
        {"wallet": b["wallet"], "slot": b["first_buy_slot"], "signature": b["first_buy_signature"],
         "received": b["received_amount"], "spent": b["spent"]}
        for b in exp["buyers"]
    ]
    assert got == want
    # у basic рівно 5 покупців за правилом: творець, ейрдроп і наповнення пулу купівлею не є
    if name == "basic":
        assert len(first) == 5


def _wallet_transfers(tx):
    """Усі розпізнані перекази транзакції: (path, sender, receiver, asset, amount)."""
    message = tx["transaction"]["message"]
    keys = [k["pubkey"] for k in message["accountKeys"]]
    instructions = [(str(i), ix) for i, ix in enumerate(message["instructions"])]
    for group in tx["meta"]["innerInstructions"]:
        instructions += [(f"{group['index']}.{j}", ix) for j, ix in enumerate(group["instructions"])]
    out = []
    for path, ix in instructions:
        parsed = ix.get("parsed")
        if not parsed:
            continue
        info = parsed["info"]
        if ix["programId"] == SYSTEM and parsed["type"] == "transfer":
            out.append((path, info["source"], info["destination"], "sol", info["lamports"]))
        elif ix["programId"] == SYSTEM and parsed["type"] == "createAccount":
            out.append((path, info["source"], info["newAccount"], "sol", info["lamports"]))
        elif ix["programId"] in TOKEN_PROGRAMS and parsed["type"] in ("transfer", "transferChecked"):
            amount = int(info["tokenAmount"]["amount"] if "tokenAmount" in info else info["amount"])
            owners, mint = {}, None
            for side in ("preTokenBalances", "postTokenBalances"):
                for tb in tx["meta"][side]:
                    owners[keys[tb["accountIndex"]]] = (tb["owner"], tb["mint"])
            sender, mint = owners[info["source"]]
            receiver, _ = owners[info["destination"]]
            out.append((path, sender, receiver, f"spl:{mint}", amount))
    return out


def _oracle_funding(rpc, buyers, depth_limit, collect_spl):
    """BFS назад з причинним відсіканням (Q1) і строгою межею-транзакцією (R-1)."""
    pos = _chronology(rpc)
    by_receiver = {}
    for sig, tx in rpc["getTransaction"].items():
        if tx is None or tx["meta"]["err"] is not None:
            continue
        for path, sender, receiver, asset, amount in _wallet_transfers(tx):
            if sender == receiver or (asset != "sol" and not collect_spl):
                continue
            by_receiver.setdefault(receiver, []).append((sig, path, sender, asset, amount))
    result = {}
    visited = {b["wallet"] for b in buyers}
    level = {b["wallet"]: pos[b["first_buy_signature"]] for b in buyers}
    for depth in range(1, depth_limit + 1):
        nxt = {}
        for wallet, cutoff in level.items():
            for sig, path, sender, asset, amount in by_receiver.get(wallet, []):
                if pos[sig] >= cutoff:
                    continue
                key = (sig, path)
                result.setdefault(key, (sig, path, sender, wallet, asset, amount, depth))
                if sender not in visited:
                    nxt[sender] = max(nxt.get(sender, -1), pos[sig])
        visited |= set(nxt)
        level = nxt
    return result


def _as_tuple(t):
    return (t["signature"], t["instruction_path"], t["sender"], t["receiver"], t["asset"], t["amount"], t["depth"])


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("collect_spl", [True, False])
def test_basic_expected_transfers_match_bruteforce_funding_oracle(depth, collect_spl):
    rpc, exp = _rpc("basic"), _expected("basic")
    oracle = _oracle_funding(rpc, exp["buyers"], depth, collect_spl)
    # еталон для d та без SPL виводиться з правил, записаних у `rules` (фільтр за depth і активом)
    want = {
        (t["signature"], t["instruction_path"]): _as_tuple(t)
        for t in exp["transfers"]
        if t["depth"] <= depth and (collect_spl or t["asset"] == "sol")
    }
    assert oracle == want


def test_hub_control_case_matches_bruteforce_funding_oracle():
    rpc, exp = _rpc("hub"), _expected("hub")
    case = dict(_cases("hub"))["hub_control"]
    oracle = _oracle_funding(rpc, exp["buyers"], case["config"]["funding_depth"], True)
    assert oracle == {(t["signature"], t["instruction_path"]): _as_tuple(t) for t in case["transfers"]}
