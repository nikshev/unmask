# verifies: FR-001-15, FR-001-16
"""HttpRpcSource: JSON-RPC 2.0 через httpx.MockTransport — жодного сокета (принцип II).

Усе, що тут моделюється «мережею», — це `httpx.MockTransport(handler)`: handler бачить справжній
`httpx.Request` (тіло, заголовки, таймаути) і повертає `httpx.Response` або кидає httpx-виняток.
Час — `FakeClock`; пауза між повторами — `sleep`, що просуває той самий годинник.
"""

import copy
import dataclasses
import json
import logging
import math
import re
import traceback
import warnings
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpcore
import httpx
import pytest

from unmask.ingest.budget import Deadline, FakeClock
from unmask.ingest.config import ConfigError, RpcConfig, load_config
from unmask.ingest.rpc.fixture import FixtureRpcSource
from unmask.ingest.rpc.http import HttpRpcSource
from unmask.ingest.serialize import to_dict
from unmask.ingest.service import IngestService
from unmask.ingest.rpc.protocol import (
    RpcBudgetTimeout,
    RpcError,
    RpcRateLimited,
    RpcTimeout,
    RpcUnavailable,
)

SECRET = "SECRET123"
URL = f"https://rpc.example/?api-key={SECRET}"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

CFG = RpcConfig(
    page_size=1000,
    request_timeout_seconds=10,
    max_retries=2,
    retry_backoff_seconds=0.5,
    max_concurrency=8,
)


def cfg(**kw) -> RpcConfig:
    fields = {**CFG.__dict__, **kw}
    return RpcConfig(**fields)


class Harness:
    """Підставна «мережа»: журнал запитів, годинник, пауза, що просуває годинник."""

    def __init__(self, handler, *, rpc_cfg=CFG, commitment="finalized", budget=40.0, url=URL, **source_kw):
        self.clock = FakeClock()
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self._handler = handler
        self.deadline = Deadline(self.clock, budget)
        self.source = HttpRpcSource(
            url,
            rpc_cfg,
            transport=httpx.MockTransport(self._dispatch),
            commitment=commitment,
            clock=self.clock,
            sleep=self._sleep,
            **source_kw,
        )

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request, self)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def ok(result, *, id=1):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": id, "result": result})


def echo_id(result_for):
    """handler для одиночного запиту: відповідає result_for(body) з тим самим id."""

    def handler(request, h):
        body = json.loads(request.content)
        return ok(result_for(body), id=body["id"])

    return handler


def sig_entry(sig, slot):
    return {"signature": sig, "slot": slot, "err": None, "memo": None, "blockTime": 1759400000 + slot,
            "confirmationStatus": "finalized"}


# --------------------------------------------------------------------------- конверт і параметри


def test_request_envelope_and_params_for_each_method():
    mint = {"lamports": 1, "owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "mint"}}}
    sigs = [sig_entry("s2", 20), sig_entry("s1", 10)]
    tx = {"slot": 10, "blockTime": 1, "transaction": {}, "meta": {}}
    acc = {"pubkey": "TA1", "account": {"data": {"parsed": {"info": {"mint": "M"}}}}}

    def handler(request, h):
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 0, "result": tx}])
        method = body["method"]
        if method == "getAccountInfo":
            result = {"context": {"slot": 1}, "value": mint}
        elif method == "getSignaturesForAddress":
            result = sigs
        else:
            result = {"context": {"slot": 1}, "value": [acc]}
        return ok(result, id=body["id"])

    h = Harness(handler)
    s, d = h.source, h.deadline

    assert s.get_account_info("MINT1", deadline=d) == mint
    assert s.get_signatures_for_address("ADDR1", before="sigB", until="sigU", limit=50, deadline=d) == sigs
    assert s.get_transactions(["s1"], deadline=d) == [tx]
    got = s.get_token_accounts_by_owner("OWNER1", deadline=d)
    assert got == [acc, acc]  # Token + Token-2022: два запити, один список

    for r in h.requests:
        assert r.method == "POST"
        assert str(r.url) == URL
        assert r.headers["content-type"].startswith("application/json")
    b = h.bodies()
    assert [x["method"] for x in b if isinstance(x, dict)] == [
        "getAccountInfo", "getSignaturesForAddress", "getTokenAccountsByOwner", "getTokenAccountsByOwner",
    ]
    assert all(x["jsonrpc"] == "2.0" for x in b if isinstance(x, dict))
    assert b[0]["params"] == ["MINT1", {"encoding": "jsonParsed", "commitment": "finalized"}]
    assert b[1]["params"] == [
        "ADDR1", {"limit": 50, "before": "sigB", "until": "sigU", "commitment": "finalized"},
    ]
    assert b[2] == [{
        "jsonrpc": "2.0", "id": 0, "method": "getTransaction",
        "params": ["s1", {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                          "commitment": "finalized"}],
    }]  # T-049: за замовчуванням 1 (було 0 — вузол відмовляв -32015 на транзакціях version 1)
    assert b[3]["params"] == ["OWNER1", {"programId": TOKEN_PROGRAM},
                              {"encoding": "jsonParsed", "commitment": "finalized"}]
    assert b[4]["params"] == ["OWNER1", {"programId": TOKEN_2022_PROGRAM},
                              {"encoding": "jsonParsed", "commitment": "finalized"}]


def test_signatures_omit_before_until_when_none():
    h = Harness(echo_id(lambda body: []))
    got = h.source.get_signatures_for_address("A", before=None, until=None, limit=1000, deadline=h.deadline)
    assert got == []
    params = h.bodies()[0]["params"]
    assert params == ["A", {"limit": 1000, "commitment": "finalized"}]


def test_before_signature_absent_from_address_history_is_passed_through_unchecked():
    # Ревʼю T-012: SPL-ребро від делегата — підпису немає в історії адреси; на живому RPC `before`
    # працює за слотом. Адаптер лише передає параметр і наявність НЕ перевіряє (один запит, без
    # додаткового getSignaturesForAddress для пошуку підпису).
    h = Harness(echo_id(lambda body: [sig_entry("old", 5)]))
    got = h.source.get_signatures_for_address(
        "A", before="sig_of_delegate_edge", until=None, limit=10, deadline=h.deadline
    )
    assert [e["signature"] for e in got] == ["old"]
    assert len(h.requests) == 1
    assert h.bodies()[0]["params"][1]["before"] == "sig_of_delegate_edge"


@pytest.mark.parametrize("commitment", ["finalized", "confirmed"])
def test_commitment_comes_from_config_for_every_method(commitment):
    def handler(request, h):
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 0, "result": None}])
        m = body["method"]
        result = {"getAccountInfo": {"value": None}, "getSignaturesForAddress": [],
                  "getTokenAccountsByOwner": {"value": []}}[m]
        return ok(result, id=body["id"])

    h = Harness(handler, commitment=commitment)
    s, d = h.source, h.deadline
    s.get_account_info("A", deadline=d)
    s.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=d)
    s.get_transactions(["x"], deadline=d)
    s.get_token_accounts_by_owner("A", deadline=d)
    commitments = []
    for b in h.bodies():
        for item in b if isinstance(b, list) else [b]:
            commitments.append([p for p in item["params"] if isinstance(p, dict)][-1]["commitment"])
    assert commitments == [commitment] * 5


def test_name_is_http():
    h = Harness(echo_id(lambda b: None))
    assert h.source.name == "http"


def test_invalid_commitment_and_url_scheme_are_rejected_without_echoing_the_url():
    with pytest.raises(ValueError):
        HttpRpcSource(URL, CFG, commitment="processed")
    bad = f"ftp://rpc.example/?api-key={SECRET}"
    with pytest.raises(ValueError) as ei:
        HttpRpcSource(bad, CFG, commitment="finalized")
    assert SECRET not in str(ei.value)
    with pytest.raises(ValueError):
        HttpRpcSource("", CFG, commitment="finalized")


# --------------------------------------------------------------------------- форми результатів


def test_account_info_missing_account_is_none():
    h = Harness(echo_id(lambda b: {"context": {"slot": 1}, "value": None}))
    assert h.source.get_account_info("A", deadline=h.deadline) is None


@pytest.mark.parametrize("result", [None, [], "x", {"context": {}}, {"value": 5}, {"value": []}])
def test_account_info_unexpected_shape_is_unavailable(result):
    h = Harness(echo_id(lambda b: result), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable):
        h.source.get_account_info("A", deadline=h.deadline)


def test_signatures_come_back_newest_first_as_received():
    entries = [sig_entry("c", 30), sig_entry("b", 30), sig_entry("a", 10)]
    h = Harness(echo_id(lambda b: entries))
    got = h.source.get_signatures_for_address("A", before=None, until=None, limit=10, deadline=h.deadline)
    assert got == entries
    assert {"signature", "slot", "blockTime", "err"} <= set(got[0])


@pytest.mark.parametrize(
    "result",
    [
        {"not": "a list"},
        [sig_entry("a", 10), sig_entry("b", 20)],  # від найстарішого: порушує контракт порядку
        [sig_entry("a", 30), sig_entry("b", 20), sig_entry("c", 10)],  # довше за limit=2
        [{"slot": 1}],  # без signature
        [{"signature": 5, "slot": 1}],
        ["sig"],
    ],
)
def test_signatures_contract_violations_are_unavailable(result):
    h = Harness(echo_id(lambda b: result), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable):
        h.source.get_signatures_for_address("A", before=None, until=None, limit=2, deadline=h.deadline)


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_signatures_limit_out_of_range_is_a_caller_defect_and_sends_nothing(limit):
    h = Harness(echo_id(lambda b: []))
    with pytest.raises(ValueError):
        h.source.get_signatures_for_address("A", before=None, until=None, limit=limit, deadline=h.deadline)
    assert h.requests == []


def test_token_accounts_merge_token_then_token2022_and_second_failure_fails_the_call():
    def handler(request, h):
        body = json.loads(request.content)
        program = body["params"][1]["programId"]
        if program == TOKEN_2022_PROGRAM and h.fail_second:
            return httpx.Response(503)
        pk = "T1" if program == TOKEN_PROGRAM else "T22"
        return ok({"value": [{"pubkey": pk, "account": {}}]}, id=body["id"])

    h = Harness(handler, rpc_cfg=cfg(max_retries=0))
    h.fail_second = False
    got = h.source.get_token_accounts_by_owner("O", deadline=h.deadline)
    assert [e["pubkey"] for e in got] == ["T1", "T22"]
    h.fail_second = True
    with pytest.raises(RpcUnavailable):  # половина списку — не результат
        h.source.get_token_accounts_by_owner("O", deadline=h.deadline)


@pytest.mark.parametrize("value", [None, {}, "x", [{"account": {}}], [5]])
def test_token_accounts_unexpected_shape_is_unavailable(value):
    h = Harness(echo_id(lambda b: {"value": value}), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable):
        h.source.get_token_accounts_by_owner("O", deadline=h.deadline)


# --------------------------------------------------------------------------- batch get_transactions


def batch_handler(results, *, shuffle=False, drop=None, extra=None, dup=None, as_dict=None):
    """Відповідь на batch: results[sig] -> RawTransaction | None | {"error": ...}."""

    def handler(request, h):
        body = json.loads(request.content)
        assert isinstance(body, list)
        out = []
        for item in body:
            sig = item["params"][0]
            r = results[sig]
            entry = {"jsonrpc": "2.0", "id": item["id"]}
            entry.update(r if isinstance(r, dict) and "error" in r else {"result": r})
            out.append(entry)
        if drop is not None:
            out = [e for e in out if e["id"] != drop]
        if extra is not None:
            out.append({"jsonrpc": "2.0", "id": extra, "result": None})
        if dup is not None:
            out.append(out[dup])
        if shuffle:
            out.reverse()
        if as_dict is not None:
            return httpx.Response(200, json=as_dict)
        return httpx.Response(200, json=out)

    return handler


def txr(n):
    return {"slot": n, "blockTime": n, "transaction": {"signatures": [f"s{n}"]}, "meta": {"err": None}}


def test_batch_get_transactions_preserves_order_and_none_for_missing():
    results = {"s1": txr(1), "s2": None, "s3": txr(3), "s4": None}
    h = Harness(batch_handler(results, shuffle=True))  # сервер вправі відповісти в іншому порядку
    got = h.source.get_transactions(["s3", "s2", "s1", "s4"], deadline=h.deadline)
    assert got == [txr(3), None, txr(1), None]
    assert len(h.requests) == 1  # один HTTP-запит з масивом JSON-RPC
    assert [i["params"][0] for i in h.bodies()[0]] == ["s3", "s2", "s1", "s4"]


def test_batch_is_chunked_by_max_batch_and_order_is_kept_across_chunks():
    # T-049: порції — за `max_batch` адаптера, а не за `rpc.page_size` (було page_size=2).
    sigs = [f"s{i}" for i in range(5)]
    results = {s: (None if i == 3 else txr(i)) for i, s in enumerate(sigs)}
    h = Harness(batch_handler(results, shuffle=True), max_batch=2)
    got = h.source.get_transactions(sigs, deadline=h.deadline)
    assert got == [txr(0), txr(1), txr(2), None, txr(4)]
    assert [len(b) for b in h.bodies()] == [2, 2, 1]  # кожен запит ≤ max_batch


def test_batch_with_duplicate_signatures_answers_per_position():
    h = Harness(batch_handler({"s1": txr(1)}))
    assert h.source.get_transactions(["s1", "s1"], deadline=h.deadline) == [txr(1), txr(1)]


def test_batch_empty_input_sends_nothing():
    h = Harness(batch_handler({}))
    assert h.source.get_transactions([], deadline=h.deadline) == []
    assert h.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"drop": 1},  # відсутній id у відповіді
        {"extra": 99},  # невідомий id
        {"dup": 0},  # id двічі
        {"as_dict": {"jsonrpc": "2.0", "id": None, "result": None}},  # не масив
    ],
)
def test_batch_response_not_matching_request_is_unavailable_never_a_guess(kwargs):
    h = Harness(batch_handler({"s1": txr(1), "s2": txr(2)}, **kwargs), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable):
        h.source.get_transactions(["s1", "s2"], deadline=h.deadline)


def test_batch_element_error_fails_whole_call_instead_of_becoming_none():
    # Рішення: помилка елемента ≠ «транзакцію не знайдено». None — це `result: null` (обрізана
    # історія), і ядро чесно позначає його як відсутню історію. JSON-RPC `error` — «не вдалось
    # дізнатись»: перетворити його на None означало б видати збій за відсутність даних (принцип V),
    # а повтор усього виклику (рядок T-021) допомагає тимчасовим збоям. Тому — RpcUnavailable на весь виклик.
    results = {"s1": txr(1), "s2": {"error": {"code": -32602, "message": "Invalid param"}}}
    h = Harness(batch_handler(results), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        h.source.get_transactions(["s1", "s2"], deadline=h.deadline)
    assert "-32602" in ei.value.detail


def test_batch_element_with_rate_limit_code_is_rate_limited_for_whole_call():
    results = {"s1": {"error": {"code": -32602, "message": "x"}}, "s2": {"error": {"code": 429, "message": "slow"}}}
    h = Harness(batch_handler(results), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcRateLimited):
        h.source.get_transactions(["s1", "s2"], deadline=h.deadline)


@pytest.mark.parametrize("bad", ["x", 5, [1], True])
def test_batch_element_result_of_wrong_type_is_unavailable(bad):
    h = Harness(batch_handler({"s1": bad}), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable):
        h.source.get_transactions(["s1"], deadline=h.deadline)


# --------------------------------------------------------------------------- T-049: версія транзакцій і під-batch


def sigs_and_results(n, *, none_at=()):
    sigs = [f"s{i}" for i in range(n)]
    return sigs, {s: (None if i in none_at else txr(i)) for i, s in enumerate(sigs)}


def tx_items(h):
    return [item for body in h.bodies() if isinstance(body, list) for item in body]


def test_max_supported_transaction_version_is_sent_and_defaults_to_1():
    sigs, results = sigs_and_results(30)
    h = Harness(batch_handler(results))  # max_tx_version за замовчуванням
    h.source.get_transactions(sigs, deadline=h.deadline)
    items = tx_items(h)
    assert len(items) == 30 and len(h.requests) == 2  # обидва під-batch
    assert {i["params"][1]["maxSupportedTransactionVersion"] for i in items} == {1}
    assert all(type(i["params"][1]["maxSupportedTransactionVersion"]) is int for i in items)
    for explicit in (0, 2, 7):
        h = Harness(batch_handler(results), max_tx_version=explicit)
        h.source.get_transactions(sigs[:3], deadline=h.deadline)
        assert [i["params"][1] for i in tx_items(h)] == [
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": explicit, "commitment": "finalized"}
        ] * 3


def test_v1_transaction_response_is_returned_unchanged():
    real_sig, real_tx = _real_v1_transaction()
    assert real_tx["version"] == 1
    pristine = copy.deepcopy(real_tx)
    rpc = {"getTransaction": {real_sig: real_tx, "legacy_sig": {**txr(5), "version": "legacy"}, "gone": None}}
    holder = {}
    h = Harness(lambda request, h: _emulator(rpc, "finalized", holder["bucket"])(request))
    holder["bucket"] = ProviderBucket(h.clock)
    got = h.source.get_transactions(["gone", real_sig, "legacy_sig"], deadline=h.deadline)
    assert got == [None, pristine, {**txr(5), "version": "legacy"}]  # дослівно, без перекладу структури
    assert got[1]["version"] == 1
    assert json.dumps(got[1], sort_keys=True) == json.dumps(pristine, sort_keys=True)


@pytest.mark.parametrize("max_batch", [1, 2, 3, 7, 8, 25, 1000, 10**6])
def test_get_transactions_splits_into_sub_batches_of_max_batch_preserving_order(max_batch):
    n = 23
    sigs, results = sigs_and_results(n, none_at=(4, 22))
    h = Harness(batch_handler(results, shuffle=True), max_batch=max_batch, burst=max(max_batch, 40))
    got = h.source.get_transactions(sigs, deadline=h.deadline)
    # ідентичність результату не залежить від max_batch
    assert got == [None if i in (4, 22) else txr(i) for i in range(n)]
    bodies = h.bodies()
    assert len(h.requests) == -(-n // max_batch)  # ceil(n / max_batch)
    assert all(isinstance(b, list) and 1 <= len(b) <= max_batch for b in bodies)
    assert [len(b) for b in bodies[:-1]] == [max_batch] * (len(bodies) - 1)  # послідовні повні під-batch
    # JSON-RPC id = позиція підпису у ВСЬОМУ виклику, а не в під-batch; підписи йдуть по порядку
    assert [i["id"] for i in tx_items(h)] == list(range(n))
    assert [i["params"][0] for i in tx_items(h)] == sigs


def test_sub_batch_with_duplicate_signatures_across_sub_batches_answers_per_position():
    h = Harness(batch_handler({"a": txr(1), "b": None}), max_batch=2)
    got = h.source.get_transactions(["a", "b", "a", "a", "b"], deadline=h.deadline)
    assert got == [txr(1), None, txr(1), txr(1), None]


@pytest.mark.parametrize("page_size", [1, 2, 40, 1000])
def test_page_size_does_not_control_transaction_batch_size(page_size):
    sigs, results = sigs_and_results(60)
    h = Harness(batch_handler(results), rpc_cfg=cfg(page_size=page_size))  # max_batch за замовчуванням: 25
    assert h.source.get_transactions(sigs, deadline=h.deadline) == [txr(i) for i in range(60)]
    assert [len(b) for b in h.bodies()] == [25, 25, 10]
    h = Harness(batch_handler(results), rpc_cfg=cfg(page_size=page_size), max_batch=40, burst=40)
    h.source.get_transactions(sigs, deadline=h.deadline)
    assert [len(b) for b in h.bodies()] == [40, 20]


UNSUPPORTED = "jsonrpc error code=-32015 (unsupported transaction version)"
CANARY_T049 = "PROVIDERCANARY_T049"
V1_ERROR = {"error": {"code": -32015, "message": f"Transaction version (1) is not supported {CANARY_T049}",
                      "data": CANARY_T049}}


def test_unsupported_transaction_version_error_is_unavailable_never_none():
    # одиночний елемент
    h = Harness(batch_handler({"s1": V1_ERROR}), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        h.source.get_transactions(["s1"], deadline=h.deadline)
    assert type(ei.value) is RpcUnavailable
    assert ei.value.detail == f"batch item 0: {UNSUPPORTED}"
    assert CANARY_T049 not in "".join(traceback.format_exception(ei.value))
    # у другому під-batch, перший під-batch успішний: увесь виклик — Unavailable з глобальною позицією
    sigs, results = sigs_and_results(5)
    results["s3"] = V1_ERROR
    h = Harness(batch_handler(results), rpc_cfg=cfg(max_retries=0), max_batch=2)
    with pytest.raises(RpcUnavailable) as ei:
        h.source.get_transactions(sigs, deadline=h.deadline)
    assert type(ei.value) is RpcUnavailable and not isinstance(ei.value, RpcRateLimited)
    assert ei.value.detail == f"batch item 3: {UNSUPPORTED}"
    # разом із `result: null` у тому ж під-batch: -32015 не стає «не знайдено»
    h = Harness(batch_handler({"a": None, "b": V1_ERROR}), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable):
        h.source.get_transactions(["a", "b"], deadline=h.deadline)
    # відмова всьому batch одним об'єктом і одиночний виклик — та сама фіксована мітка
    h = Harness(batch_handler({"s1": txr(1)}, as_dict={"jsonrpc": "2.0", "id": None, **V1_ERROR}),
                rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        h.source.get_transactions(["s1"], deadline=h.deadline)
    assert ei.value.detail == UNSUPPORTED and ei.value.args == (UNSUPPORTED,)
    h = Harness(one_shot(_err_body(V1_ERROR["error"])), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert ei.value.detail == UNSUPPORTED
    assert CANARY_T049 not in "".join(traceback.format_exception(ei.value))


BAD_VERSIONS = [-1, -100, True, False, 1.0, "1", None, [1]]
BAD_BATCHES = [0, -1, True, False, 25.0, "25", None, [25]]


@pytest.mark.parametrize("kw", [{"max_tx_version": v} for v in BAD_VERSIONS]
                         + [{"max_batch": v} for v in BAD_BATCHES])
def test_max_batch_and_max_tx_version_validation(kw):
    requests = []
    transport = httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200))
    with pytest.raises(ValueError) as ei:
        HttpRpcSource(URL, CFG, transport=transport, commitment="finalized", **kw)
    exc = ei.value
    assert type(exc) is ValueError
    name = next(iter(kw))
    assert name in str(exc)
    rendered = "".join(traceback.format_exception(exc))
    assert SECRET not in rendered and "rpc.example" not in rendered
    assert exc.__cause__ is None and exc.__context__ is None
    assert requests == []


def test_max_batch_and_max_tx_version_valid_boundaries_and_keyword_only():
    for kw in ({"max_tx_version": 0}, {"max_tx_version": 1}, {"max_tx_version": 2**31},
               {"max_batch": 1}, {"max_batch": 10**9, "burst": 10**9}):
        HttpRpcSource(URL, CFG, commitment="finalized", **kw)
    with pytest.raises(TypeError):
        HttpRpcSource(URL, CFG, None, "finalized")  # commitment, max_* — лише keyword
    src = HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": URL},
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[
                                     {"jsonrpc": "2.0", "id": i["id"], "result": None}
                                     for i in json.loads(r.content)])),
                                 max_tx_version=3, max_batch=2, rate_per_second=math.inf)
    assert src.get_transactions(["a", "b", "c"], deadline=Deadline(FakeClock(), 10)) == [None] * 3
    # прокидання перевіряє `test_from_env_passes_every_adapter_parameter_through_observably` за вмістом запитів
    with pytest.raises(ValueError):
        HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": URL}, max_batch=0)


@pytest.mark.parametrize("failure", ["item_error", "http_503", "foreign_id", "rate_limit_item", "timeout"])
def test_sub_batch_failure_fails_whole_call_all_or_nothing(failure):
    sigs, results = sigs_and_results(6)

    def handler(request, h):
        body = json.loads(request.content)
        ids = [i["id"] for i in body]
        if ids == [2, 3]:  # другий під-batch
            if failure == "item_error":
                return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 2, "result": txr(2)},
                                                 {"jsonrpc": "2.0", "id": 3, "error": {"code": -32000}}])
            if failure == "http_503":
                return httpx.Response(503)
            if failure == "foreign_id":
                return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 0, "result": txr(0)},
                                                 {"jsonrpc": "2.0", "id": 1, "result": txr(1)}])
            if failure == "rate_limit_item":
                return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 2, "result": txr(2)},
                                                 {"jsonrpc": "2.0", "id": 3, "error": {"code": -32005}}])
            raise httpx.ReadTimeout("t")
        return batch_handler(results)(request, h)

    h = Harness(handler, rpc_cfg=cfg(max_retries=0), max_batch=2)
    expected = {"rate_limit_item": RpcRateLimited, "timeout": RpcTimeout}.get(failure, RpcUnavailable)
    got = None
    with pytest.raises(expected):
        got = h.source.get_transactions(sigs, deadline=h.deadline)
    assert got is None  # жодного часткового результату
    assert len(h.requests) == 2  # третій під-batch не відправлено


def test_deadline_expiring_between_sub_batches_raises_budget_and_sends_nothing_more():
    sigs, results = sigs_and_results(6)
    holder = {}

    class D:  # бюджет спливає рівно після першого під-batch
        def expired(self):
            return len(holder["h"].requests) >= 1

        def remaining(self):
            return 30.0

        def request_timeout(self, cap):
            return 5.0

    h = Harness(batch_handler(results), max_batch=2)
    holder["h"] = h
    with pytest.raises(RpcTimeout) as ei:
        h.source.get_transactions(sigs, deadline=D())
    assert str(ei.value) == "budget"
    assert len(h.requests) == 1 and h.sleeps == []


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan")])
def test_request_timeout_running_out_between_sub_batches_raises_budget_and_sends_nothing_more(value):
    sigs, results = sigs_and_results(6)
    holder = {}

    class D:
        def expired(self):
            return False

        def remaining(self):
            return 30.0

        def request_timeout(self, cap):
            return 5.0 if not holder["h"].requests else value

    h = Harness(batch_handler(results), max_batch=2)
    holder["h"] = h
    with pytest.raises(RpcTimeout) as ei:
        h.source.get_transactions(sigs, deadline=D())
    assert str(ei.value) == "budget"
    assert len(h.requests) == 1


def test_sub_batch_deadline_with_real_clock_burn_between_sub_batches():
    # FakeClock: перший під-batch «з'їдає» весь бюджет рівно до межі (не перевищуючи таймаут запиту)
    sigs, results = sigs_and_results(4)
    inner = batch_handler(results)

    def burn(request, h):
        h.clock.advance(50.0)
        return inner(request, h)

    h = Harness(burn, budget=50.0, rpc_cfg=cfg(request_timeout_seconds=1000), max_batch=2)
    with pytest.raises(RpcTimeout) as ei:
        h.source.get_transactions(sigs, deadline=h.deadline)
    assert str(ei.value) == "budget"
    assert len(h.requests) == 1


@pytest.mark.parametrize("failure", ["http_429", "http_429_retry_after", "http_503"])
def test_429_on_a_sub_batch_retries_only_that_sub_batch(failure):
    sigs, results = sigs_and_results(6)
    inner = batch_handler(results)
    state = {"failed": False}

    def handler(request, h):
        body = json.loads(request.content)
        if [i["id"] for i in body] == [2, 3] and not state["failed"]:
            state["failed"] = True
            return {"http_429": httpx.Response(429),
                    "http_429_retry_after": httpx.Response(429, headers={"Retry-After": "2"}),
                    "http_503": httpx.Response(503)}[failure]
        return inner(request, h)

    h = Harness(handler, max_batch=2)
    assert h.source.get_transactions(sigs, deadline=h.deadline) == [txr(i) for i in range(6)]
    assert [[i["id"] for i in b] for b in h.bodies()] == [[0, 1], [2, 3], [2, 3], [4, 5]]
    assert h.sleeps == ([2.0] if failure == "http_429_retry_after" else [0.5])


@pytest.mark.parametrize("shape", ["item", "object"])
def test_jsonrpc_rate_limit_in_a_sub_batch_body_is_not_retried_as_before(shape):
    # Повтори «як зараз» (T-049 їх не змінює): повторюються лише збої рівня HTTP/транспорту; JSON-RPC
    # помилка в тілі HTTP 200 (також -32005) одразу піднімається на весь виклик, решта не відправляється.
    sigs, results = sigs_and_results(6)
    inner = batch_handler(results)

    def handler(request, h):
        body = json.loads(request.content)
        if [i["id"] for i in body] == [2, 3]:
            if shape == "object":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": None, "error": {"code": -32005}})
            return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": 2, "error": {"code": -32005}},
                                             {"jsonrpc": "2.0", "id": 3, "result": txr(3)}])
        return inner(request, h)

    h = Harness(handler, max_batch=2)
    with pytest.raises(RpcRateLimited):
        h.source.get_transactions(sigs, deadline=h.deadline)
    assert [[i["id"] for i in b] for b in h.bodies()] == [[0, 1], [2, 3]]
    assert h.sleeps == []


@pytest.mark.parametrize("max_batch", [1, 3, 10**6])
def test_empty_signature_list_sends_no_request_for_any_max_batch(max_batch):
    h = Harness(batch_handler({}), max_batch=max_batch, burst=max(max_batch, 40))
    assert h.source.get_transactions([], deadline=h.deadline) == []
    assert h.requests == []


def test_max_batch_one_sends_one_request_per_signature():
    sigs, results = sigs_and_results(4, none_at=(1,))
    h = Harness(batch_handler(results), max_batch=1)
    assert h.source.get_transactions(sigs, deadline=h.deadline) == [txr(0), None, txr(2), txr(3)]
    assert [[(i["id"], i["params"][0]) for i in b] for b in h.bodies()] == [
        [(0, "s0")], [(1, "s1")], [(2, "s2")], [(3, "s3")]]


def test_very_large_max_batch_sends_everything_in_one_request():
    sigs, results = sigs_and_results(1000)
    h = Harness(batch_handler(results, shuffle=True), max_batch=10**6, burst=10**6)
    assert h.source.get_transactions(sigs, deadline=h.deadline) == [txr(i) for i in range(1000)]
    assert len(h.requests) == 1 and len(h.bodies()[0]) == 1000


# --------------------------------------------------------------------------- T-049: пейсер getTransaction (кошик токенів)
#
# Живий провайдер (ревʼю T-049, 16 живих прогонів): кошик токенів на кількість getTransaction-ЕЛЕМЕНТІВ —
# ємність ≈ 40 (±3), поповнення ≈ 15–16/с; за нестачі — HTTP 429 з тілом {"error":{"code":-32005}} без
# Retry-After. Емулятор — ця жива модель: ємність 40, 15.5/с (НЕ поблажливіша: старі дефолти 20/40 на ньому
# червоніють, як і на живому вузлі). Дефолти адаптера: 12/с, кошик 30, max_batch 25.

PROVIDER_CAPACITY = 40
PROVIDER_RATE = 15.5


class ProviderBucket:
    """Кошик токенів провайдера на тому ж FakeClock, що й адаптер (пауза адаптера просуває його)."""

    def __init__(self, clock, *, capacity=PROVIDER_CAPACITY, rate=PROVIDER_RATE):
        self.clock, self.capacity, self.rate = clock, capacity, rate
        self.tokens = float(capacity)
        self.at = clock.monotonic()
        self.accepted: list[tuple[float, int]] = []
        self.rejected: list[tuple[float, int]] = []

    def take(self, k: int) -> bool:
        now = self.clock.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if k > self.tokens + 1e-9:
            self.rejected.append((now, k))
            return False
        self.tokens -= k
        self.accepted.append((now, k))
        return True


RATE_LIMITED_429 = {"jsonrpc": "2.0", "id": None, "error": {"code": -32005, "message": "Too many requests"}}


def bucket_handler(results, bucket_holder):
    """batch_handler за кошиком провайдера: за нестачі токенів — HTTP 429 з тілом -32005."""
    inner = batch_handler(results, shuffle=True)

    def handler(request, h):
        body = json.loads(request.content)
        if isinstance(body, list) and not bucket_holder["bucket"].take(len(body)):
            return httpx.Response(429, json=RATE_LIMITED_429)
        return inner(request, h)

    return handler


def paced(n=150, **source_kw):
    sigs, results = sigs_and_results(n)
    holder = {}
    h = Harness(bucket_handler(results, holder), **source_kw)
    holder["bucket"] = ProviderBucket(h.clock)
    return h, holder["bucket"], sigs


def test_pacer_150_signatures_against_provider_bucket_never_hits_429():
    h, bucket, sigs = paced(150)  # усі параметри — за замовчуванням: 12/с, кошик 30, max_batch 25
    assert h.source.get_transactions(sigs, deadline=h.deadline) == [txr(i) for i in range(150)]
    assert bucket.rejected == []
    assert [len(b) for b in h.bodies()] == [25] * 6
    # віртуальний час: (150 − 30) / 12 = 10 с — не менше (інакше швидше за 12/с) і не більше (без зайвих пауз)
    assert h.clock.monotonic() == pytest.approx(10.0, abs=1e-9)
    assert sum(h.sleeps) == pytest.approx(10.0, abs=1e-9)
    # паузи за формулою (k − tokens) / rate: 30 − 25 = 5 -> (25 − 5)/12; далі щоразу 25/12
    assert h.sleeps == pytest.approx([20 / 12] + [25 / 12] * 4)


def test_old_defaults_20_per_second_burst_40_hit_429_on_the_honest_emulator():
    # Доводить, що емулятор нарешті не поблажливий: старі дефолти на живому вузлі давали 429 у кожному прогоні.
    h, bucket, sigs = paced(150, rate_per_second=20.0, burst=40, rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcRateLimited):
        h.source.get_transactions(sigs, deadline=h.deadline)
    assert len(bucket.rejected) == 1 and len(bucket.accepted) == 1  # другий під-batch: 15 + 0.5·15.5 < 25


def test_defaults_keep_a_margin_below_the_measured_provider_model():
    h, bucket, sigs = paced(150)
    h.source.get_transactions(sigs, deadline=h.deadline)
    # найменший залишок кошика провайдера після кожного прийнятого під-batch — запас, а не впритул
    assert bucket.rejected == [] and bucket.tokens >= 10.0


def test_without_pacer_the_same_provider_bucket_answers_429():
    # доводить, що емулятор справді обмежує, і тест вище не вхолосту
    h, bucket, sigs = paced(150, rate_per_second=math.inf, rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcRateLimited):
        h.source.get_transactions(sigs, deadline=h.deadline)
    assert len(bucket.rejected) == 1 and len(h.requests) == 2 and h.sleeps == []
    h, bucket, sigs = paced(150, rate_per_second=math.inf)  # навіть із повторами
    try:
        h.source.get_transactions(sigs, deadline=h.deadline)
    except RpcRateLimited:
        pass
    assert bucket.rejected


def test_eight_sequential_calls_of_25_through_the_pacer_get_no_429():
    h, bucket, _ = paced(25)
    sigs = [f"s{i}" for i in range(25)]
    for _ in range(8):
        assert h.source.get_transactions(sigs, deadline=Deadline(h.clock, 40.0)) == [txr(i) for i in range(25)]
    assert bucket.rejected == [] and len(bucket.accepted) == 8
    assert h.clock.monotonic() == pytest.approx((8 * 25 - 30) / 12.0)  # ≈ 14.17 с


def test_refill_is_capped_by_burst_after_a_long_idle():
    h, bucket, sigs = paced(150)
    h.clock.advance(1000.0)  # простій: кошик повний, але не більше burst
    bucket.tokens, bucket.at = PROVIDER_CAPACITY, h.clock.monotonic()
    start = h.clock.monotonic()
    h.source.get_transactions(sigs, deadline=Deadline(h.clock, 40.0))
    assert bucket.rejected == []
    assert h.clock.monotonic() - start == pytest.approx(10.0)


def test_tokens_are_debited_before_the_request_is_sent():
    seen = []
    sigs, results = sigs_and_results(50)
    inner = batch_handler(results)

    def handler(request, h):
        seen.append(h.source._tokens)
        return inner(request, h)

    h = Harness(handler)
    h.source.get_transactions(sigs, deadline=h.deadline)
    assert seen == pytest.approx([5.0, 0.0])  # 30 − 25; після паузи 20/12 с: 5 + 20 − 25


def test_wait_not_fitting_the_deadline_is_budget_with_no_request_no_sleep_and_untouched_tokens():
    h, bucket, sigs = paced(50, rpc_cfg=cfg(request_timeout_seconds=1000))
    h.source.get_transactions(sigs[:25], deadline=h.deadline)  # лишилось 5 токенів
    tokens, at = h.source._tokens, h.source._refilled_at
    h.clock.advance(0.25)  # поповнення +3 (обчислюється, але при відмові НЕ записується)
    before = len(h.requests)
    need = (25 - 8) / 12
    short = Deadline(h.clock, need)  # потрібно (25 − 8) / 12 с ≥ remaining
    with pytest.raises(RpcTimeout) as ei:
        h.source.get_transactions(sigs[25:], deadline=short)
    assert str(ei.value) == "budget"
    assert len(h.requests) == before and h.sleeps == []
    assert (h.source._tokens, h.source._refilled_at) == (tokens, at)
    # трохи більше бюджету — дочекались і відправили
    ok_deadline = Deadline(h.clock, need + 0.01)
    assert h.source.get_transactions(sigs[25:], deadline=ok_deadline) == [txr(i) for i in range(25, 50)]
    assert h.sleeps == pytest.approx([need])


def test_wait_fitting_only_some_sub_batches_stops_at_the_first_that_does_not_fit():
    h, bucket, sigs = paced(150)
    with pytest.raises(RpcTimeout) as ei:
        h.source.get_transactions(sigs, deadline=Deadline(h.clock, 4.0))  # 20/12 + 25/12 = 3.75; наступна 25/12 ні
    assert str(ei.value) == "budget"
    assert len(h.requests) == 3 and h.sleeps == pytest.approx([20 / 12, 25 / 12]) and bucket.rejected == []


def test_provider_429_zeroes_local_tokens_and_the_retry_goes_through_the_pacer():
    sigs, results = sigs_and_results(25)
    inner = batch_handler(results)
    state = {"n": 0}

    def handler(request, h):
        state["n"] += 1
        if state["n"] == 1:  # провайдер має менший кошик, ніж думає клієнт
            return httpx.Response(429, json=RATE_LIMITED_429)
        return inner(request, h)

    h = Harness(handler)  # max_retries=2, backoff 0.5
    assert h.source.get_transactions(sigs, deadline=h.deadline) == [txr(i) for i in range(25)]
    # 429 -> токени 0; backoff 0.5 с поповнив 6; пейсер дочекався ще (25 − 6) / 12 с
    assert h.sleeps == pytest.approx([0.5, 19 / 12])
    assert len(h.requests) == 2
    assert h.source._tokens == pytest.approx(0.0)


def test_provider_429_beyond_max_retries_is_rate_limited_and_tokens_stay_zeroed():
    h = Harness(one_shot(httpx.Response(429, json=RATE_LIMITED_429)), rpc_cfg=cfg(max_retries=1))
    with pytest.raises(RpcRateLimited):
        h.source.get_transactions([f"s{i}" for i in range(25)], deadline=h.deadline)
    assert len(h.requests) == 2  # max_retries + 1
    assert h.sleeps == pytest.approx([0.5, 19 / 12])
    assert h.source._tokens == pytest.approx(0.0)


def test_pacer_is_applied_to_other_methods_too():
    # T-053 (було `test_pacer_is_not_applied_to_other_methods`): ліміт провайдера — один на всі методи.
    h = Harness(echo_id(lambda b: {"context": {"slot": 1}, "value": None}), burst=1, max_batch=1,
                rate_per_second=0.1)
    forever = Deadline(h.clock, math.inf)
    for _ in range(5):
        h.source.get_account_info("A", deadline=forever)
    assert h.sleeps == pytest.approx([10.0] * 4) and len(h.requests) == 5


BAD_RATES = [0, 0.0, 0.05, 0.0999, 1e-300, -1.0, float("nan"), -math.inf, True, False, "20", None]
BAD_BURSTS = [0, -1, True, False, 40.0, "40", None]
PACER_BAD = (
    [({"rate_per_second": v}, "rate_per_second:") for v in BAD_RATES]
    + [({"burst": v, "max_batch": 1}, "burst:") for v in BAD_BURSTS]  # max_batch=1: лише власна перевірка
    + [({"max_batch": 31}, "max_batch:"), ({"max_batch": 26, "burst": 25}, "max_batch:")]
)


@pytest.mark.parametrize("kw, prefix", PACER_BAD)
def test_pacer_parameter_validation(kw, prefix):
    with pytest.raises(ValueError) as ei:
        HttpRpcSource(URL, CFG, commitment="finalized", **kw)
    exc = ei.value
    assert type(exc) is ValueError
    assert str(exc).startswith(prefix)
    if prefix == "max_batch:":
        assert "burst" in str(exc)
    rendered = "".join(traceback.format_exception(exc))
    assert SECRET not in rendered and "rpc.example" not in rendered
    assert exc.__cause__ is None and exc.__context__ is None
    for name, value in kw.items():  # значення в тексті немає
        if not isinstance(value, (bool, type(None))) and value not in (0, 1):
            assert repr(value) not in str(exc)


def test_pacer_parameter_valid_boundaries():
    for kw in ({"rate_per_second": math.inf}, {"rate_per_second": 0.1}, {"rate_per_second": 5},
               {"burst": 25}, {"burst": 1, "max_batch": 1}, {"max_batch": 30},
               {"max_batch": 40, "burst": 40}, {"max_batch": 100, "burst": 100}):
        HttpRpcSource(URL, CFG, commitment="finalized", **kw)


def test_from_env_passes_every_adapter_parameter_through_observably():
    clock = FakeClock()
    sleeps = []
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": i["id"], "result": None} for i in body])

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    src = HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": URL},
                                 transport=httpx.MockTransport(handler), clock=clock, sleep=sleep,
                                 max_tx_version=3, max_batch=2, rate_per_second=4.0, burst=3)
    assert src.get_transactions(list("abcdef"), deadline=Deadline(clock, 100)) == [None] * 6
    assert [len(b) for b in bodies] == [2, 2, 2]  # max_batch
    assert {i["params"][1]["maxSupportedTransactionVersion"] for b in bodies for i in b} == {3}
    # кошик 3, 4/с: 3−2=1; чекати (2−1)/4=0.25; 0; чекати 2/4=0.5
    assert sleeps == pytest.approx([0.25, 0.5])


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ConnectError("x")], ids=["http_503", "connect_error"])
def test_5xx_and_network_errors_do_not_zero_the_bucket_but_429_does(failure):
    sigs, results = sigs_and_results(25)
    inner = batch_handler(results)

    def handler_for(first):
        state = {"n": 0}

        def handler(request, h):
            state["n"] += 1
            if state["n"] == 1:
                if isinstance(first, Exception):
                    raise first
                return first
            return inner(request, h)

        return handler

    h = Harness(handler_for(failure))
    h.source.get_transactions(sigs, deadline=h.deadline)
    # 30 − 25 = 5 лишилось (не обнулено); backoff 0.5 с: +6 = 11; пейсер: (25 − 11) / 12
    assert h.sleeps == pytest.approx([0.5, 14 / 12])
    h = Harness(handler_for(httpx.Response(429, json=RATE_LIMITED_429)))
    h.source.get_transactions(sigs, deadline=h.deadline)
    assert h.sleeps == pytest.approx([0.5, 19 / 12])  # 429 обнуляє: 0 + 6, пейсер (25 − 6) / 12


def test_single_pacer_pause_is_bounded_by_burst_over_min_rate_even_with_an_infinite_deadline():
    # нижня межа швидкості 0.1/с: пауза ≤ burst / 0.1 с, навіть коли дедлайн безкінечний
    sigs, results = sigs_and_results(3)
    h = Harness(batch_handler(results), rate_per_second=0.1, burst=1, max_batch=1)
    forever = Deadline(h.clock, math.inf)
    assert h.source.get_transactions(sigs, deadline=forever) == [txr(i) for i in range(3)]
    assert h.sleeps == pytest.approx([10.0, 10.0])
    assert max(h.sleeps) <= 1 / 0.1


def test_budget_ending_in_the_pacer_after_a_429_surfaces_as_budget_timeout():
    # 429 обнулив кошик; пауза backoff вміщується, але пейсер дочекатись уже не встигне -> RpcTimeout("budget")
    h = Harness(one_shot(httpx.Response(429, json=RATE_LIMITED_429)), budget=1.0)
    with pytest.raises(RpcTimeout) as ei:
        h.source.get_transactions([f"s{i}" for i in range(25)], deadline=h.deadline)
    assert str(ei.value) == "budget"
    assert len(h.requests) == 1 and h.sleeps == [0.5]


# --------------------------------------------------------------------------- RpcBudgetTimeout (T-051)
# Усі три місця, де адаптер вирішує «винен бюджет» (пейсер не вміщається; `expired()` перед запитом;
# `request_timeout() <= 0`/NaN), піднімають саме `RpcBudgetTimeout` — ядро мапить його в `budget_exhausted`
# безумовно (навіть коли `expired()` ще хибне). Транспортні таймаути лишаються звичайним `RpcTimeout`.


def test_rpc_budget_timeout_is_an_rpc_timeout():
    exc = RpcBudgetTimeout()
    assert issubclass(RpcBudgetTimeout, RpcTimeout) and isinstance(exc, RpcTimeout) and isinstance(exc, RpcError)
    assert not isinstance(exc, (RpcRateLimited, RpcUnavailable))
    assert str(exc) == "budget" and exc.args == ("budget",)  # фіксований текст: нічого ззовні
    try:  # зворотна сумісність: старі `except RpcTimeout` ловлять і його
        raise exc
    except RpcTimeout as caught:
        assert caught is exc


def test_pacer_wait_not_fitting_raises_rpc_budget_timeout_subclass():
    h, bucket, sigs = paced(50, rpc_cfg=cfg(request_timeout_seconds=1000))
    h.source.get_transactions(sigs[:25], deadline=h.deadline)  # лишилось 5 токенів
    before = len(h.requests)
    short = Deadline(h.clock, 1.0)  # потрібно (25 − 5) / 12 ≈ 1.67 с > 1 с, але дедлайн ще живий
    with pytest.raises(RpcBudgetTimeout) as ei:
        h.source.get_transactions(sigs[25:], deadline=short)
    assert type(ei.value) is RpcBudgetTimeout and str(ei.value) == "budget"
    assert not short.expired() and short.remaining() == 1.0
    assert len(h.requests) == before and h.sleeps == []
    assert ei.value.__cause__ is None and ei.value.__context__ is None
    # і після 429: пейсер не дочекається -> теж RpcBudgetTimeout (а не RpcRateLimited)
    h2 = Harness(one_shot(httpx.Response(429, json=RATE_LIMITED_429)), budget=1.0)
    with pytest.raises(RpcBudgetTimeout) as ei2:
        h2.source.get_transactions([f"s{i}" for i in range(25)], deadline=h2.deadline)
    assert type(ei2.value) is RpcBudgetTimeout and not h2.deadline.expired()


def test_expired_deadline_and_nonpositive_request_timeout_raise_rpc_budget_timeout():
    # (1) expired() перед запитом — для кожного методу
    h = Harness(good, budget=5.0)
    h.clock.advance(5.0)
    s, d = h.source, h.deadline
    for c in (
        lambda: s.get_account_info("A", deadline=d),
        lambda: s.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=d),
        lambda: s.get_transactions(["x"], deadline=d),
        lambda: s.get_token_accounts_by_owner("A", deadline=d),
    ):
        with pytest.raises(RpcBudgetTimeout) as ei:
            c()
        assert type(ei.value) is RpcBudgetTimeout and str(ei.value) == "budget"
    assert h.requests == []

    # (2) request_timeout() <= 0 або NaN, хоча expired() хибне — для кожного методу
    for value in (0.0, -1.0, float("nan")):
        class D(_ZeroTimeoutDeadline):
            def request_timeout(self, cap, _v=value):
                return _v

        h = Harness(good, rate_per_second=math.inf)
        s = h.source
        for c in (
            lambda: s.get_account_info("A", deadline=D()),
            lambda: s.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=D()),
            lambda: s.get_transactions(["x"], deadline=D()),
            lambda: s.get_token_accounts_by_owner("A", deadline=D()),
        ):
            with pytest.raises(RpcBudgetTimeout) as ei:
                c()
            assert type(ei.value) is RpcBudgetTimeout
        assert h.requests == []


@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), httpx.WriteTimeout("t"), httpx.PoolTimeout("t"),
])
def test_transport_timeouts_stay_plain_rpc_timeout_not_budget(exc):
    h = Harness(one_shot(exc))
    with pytest.raises(RpcTimeout) as ei:
        call(h)
    assert type(ei.value) is RpcTimeout  # таймаут запиту до межі — звичайний `timeout`, не бюджет


def test_total_request_duration_over_timeout_stays_plain_rpc_timeout():
    def slow(request, h):
        h.clock.advance(11.0)  # довше за request_timeout=10, але бюджет 40 ще є
        return good(request, h)

    h = Harness(slow)
    with pytest.raises(RpcTimeout) as ei:
        call(h)
    assert type(ei.value) is RpcTimeout


def test_pacer_budget_cut_before_deadline_expiry_is_budget_exhausted_through_the_service():
    # Відтворення знахідки живого прогону: дедлайн сервісу живий (годинник стоїть), але пейсер каже
    # «не вмістимось» -> buyers.reason = budget_exhausted (а не timeout); detail — мітка без ключа й URL.
    directory = SCENARIOS / "basic"
    rpc = json.loads((directory / "rpc.json").read_text())
    expected = json.loads((directory / "expected.json").read_text())
    config = dataclasses.replace(
        load_config(Path(__file__).parent.parent / "config" / "ingest.yaml"),
        **expected["config"], time_budget_seconds=5.0,
    )
    source_clock = FakeClock()
    bucket = ProviderBucket(source_clock)
    http_source = HttpRpcSource(
        URL, config.rpc, transport=httpx.MockTransport(_emulator(rpc, config.commitment, bucket)),
        commitment=config.commitment, clock=source_clock, sleep=source_clock.advance,
        # T-053: кошик спільний для всіх методів — запас на звернення до першого getTransaction
        # (getAccountInfo + getSignaturesForAddress = 2 одиниці) + одна транзакція; друга — через 10 с > бюджету 5 с
        max_batch=1, burst=3, rate_per_second=0.1,
    )
    service_clock = FakeClock()  # стоїть: deadline.expired() так і не стає істинним
    result = IngestService(config, http_source, clock=service_clock).collect(expected["mint"])
    out = to_dict(result)
    b = out["completeness"]["buyers"]
    assert (b["complete"], b["reason"]) == (False, "budget_exhausted")
    assert "getTransaction" in b["detail"] and "budget" in b["detail"]
    reasons = {m["reason"] for m in out["completeness"]["missing"]} | {b["reason"]}
    assert "timeout" not in reasons
    rendered = json.dumps(out)
    assert SECRET not in rendered and "rpc.example" not in rendered and "://" not in b["detail"]
    assert bucket.rejected == []


# --------------------------------------------------------------------------- мапування помилок


def one_shot(response_or_exc):
    def handler(request, h):
        if isinstance(response_or_exc, Exception):
            raise response_or_exc
        return response_or_exc

    return handler


def call(h):
    return h.source.get_account_info("A", deadline=h.deadline)


def test_429_maps_to_rate_limited_with_retry_after():
    h = Harness(one_shot(httpx.Response(429, headers={"Retry-After": "7"})), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcRateLimited) as ei:
        call(h)
    assert ei.value.retry_after == 7.0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Retry-After": ""},
        {"Retry-After": "soon"},
        {"Retry-After": "-5"},
        {"Retry-After": "nan"},
        {"Retry-After": "inf"},
        {"Retry-After": "1e2"},
        {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},  # HTTP-date не підтримуємо: лише секунди
    ],
)
def test_429_without_valid_retry_after_gives_none(headers):
    h = Harness(one_shot(httpx.Response(429, headers=headers)), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcRateLimited) as ei:
        call(h)
    assert ei.value.retry_after is None


def test_retry_after_zero_and_fraction_are_valid():
    for raw, want in (("0", 0.0), ("1.5", 1.5)):
        h = Harness(one_shot(httpx.Response(429, headers={"Retry-After": raw})), rpc_cfg=cfg(max_retries=0))
        with pytest.raises(RpcRateLimited) as ei:
            call(h)
        assert ei.value.retry_after == want


def test_429_after_retries_exhausted_raises_rate_limited_after_max_retries_plus_one_requests():
    h = Harness(one_shot(httpx.Response(429)), rpc_cfg=cfg(max_retries=3, retry_backoff_seconds=0.1))
    with pytest.raises(RpcRateLimited):
        call(h)
    assert len(h.requests) == 4
    assert h.sleeps == pytest.approx([0.1, 0.2, 0.4])


def test_retry_after_longer_than_backoff_is_honoured_as_the_pause():
    answers = [httpx.Response(429, headers={"Retry-After": "3"}), None]

    def handler(request, h):
        a = answers.pop(0)
        return a if a is not None else ok({"value": None}, id=json.loads(request.content)["id"])

    h = Harness(handler)
    assert call(h) is None
    assert h.sleeps == [3.0]  # max(backoff 0.5, retry_after 3)


@pytest.mark.parametrize("code", [429, -32005])
def test_jsonrpc_rate_limit_code_maps_to_rate_limited(code):
    def handler(request, h):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "slow down"}})

    h = Harness(handler, rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcRateLimited) as ei:
        call(h)
    assert ei.value.retry_after is None


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        httpx.WriteTimeout("t"),
        httpx.PoolTimeout("t"),
    ],
)
def test_timeout_maps_to_rpc_timeout_and_is_not_retried(exc):
    h = Harness(one_shot(exc))
    with pytest.raises(RpcTimeout):
        call(h)
    assert len(h.requests) == 1  # бюджет на запит уже витрачено — повтор не допомагає
    assert h.sleeps == []


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("bad framing"),
        httpx.ProxyError("proxy"),
        httpx.DecodingError("gzip"),
    ],
)
def test_network_failures_map_to_unavailable(exc):
    h = Harness(one_shot(exc), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable):
        call(h)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(503),
        httpx.Response(404),
        httpx.Response(403),
        httpx.Response(301, headers={"Location": "https://elsewhere.example/"}),  # редирект не йдемо: ключ
        httpx.Response(  # 3xx з ВАЛІДНИМ JSON-тілом — усе одно не успіх
            301, json={"jsonrpc": "2.0", "id": 1, "result": {"value": None}},
            headers={"Location": "https://elsewhere.example/"}),
        httpx.Response(199, json={"jsonrpc": "2.0", "id": 1, "result": {"value": None}}),  # 1xx-подібне: не 2xx
        httpx.Response(200, text="<html>not json"),
        httpx.Response(200, content=b"\xff\xfe\x00"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json="x"),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1}),  # ні result, ні error
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 777, "result": {"value": None}}),  # чужий id
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": "weird"}),
    ],
)
def test_5xx_and_jsonrpc_error_map_to_unavailable(response):
    h = Harness(one_shot(response), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert ei.value.detail
    assert len(h.requests) == 1


def test_jsonrpc_error_detail_carries_code_and_fixed_label_but_never_the_provider_message():
    # Політика detail (ескалація T-021): лише категорія з allow-list; вільний текст провайдера не потрапляє.
    resp = httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}})
    h = Harness(one_shot(resp), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert ei.value.detail == "jsonrpc error code=-32000 (server error)"
    assert "boom" not in str(ei.value)


def test_foreign_exceptions_from_transport_are_not_swallowed():
    # Контракт: усе поза ієрархією RpcError — дефект (ValueError тощо) і виходить назовні гучно,
    # а не маскується під «недоступно». Мережеві збої httpx — штатні й мапляться (тести вище).
    h = Harness(one_shot(ValueError("bug")))
    with pytest.raises(ValueError):
        call(h)


# --------------------------------------------------------------------------- повтори й дедлайн


def flaky(failures, then):
    """Спершу відповіді з `failures` (Response або виняток), далі — then(request)."""
    queue = list(failures)

    def handler(request, h):
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return then(request)

    return handler


def good(request, h=None):
    return ok({"value": {"lamports": 1}}, id=json.loads(request.content)["id"])


def test_retries_then_succeeds_within_deadline():
    h = Harness(flaky([httpx.Response(503), httpx.ConnectError("x")], good))
    assert call(h) == {"lamports": 1}
    assert len(h.requests) == 3
    assert h.sleeps == [0.5, 1.0]  # експоненційна пауза: retry_backoff_seconds * 2**n


def test_rate_limited_then_unavailable_then_success_all_retried():
    h = Harness(flaky([httpx.Response(429), httpx.Response(500)], good))
    assert call(h) == {"lamports": 1}
    assert len(h.requests) == 3


def test_unavailable_after_retries_exhausted_raises_last_error():
    h = Harness(flaky([httpx.Response(500)] * 5, good), rpc_cfg=cfg(max_retries=2))
    with pytest.raises(RpcUnavailable):
        call(h)
    assert len(h.requests) == 3


def test_zero_retries_means_a_single_request_and_no_sleep():
    h = Harness(one_shot(httpx.Response(500)), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable):
        call(h)
    assert len(h.requests) == 1 and h.sleeps == []


def test_pause_never_exceeds_remaining_and_retry_not_attempted_when_it_cannot_fit():
    # бюджет 0.8 с, пауза 0.5 → 1.0: перша пауза вміщується, друга (1.0 ≥ 0.3) — ні
    h = Harness(one_shot(httpx.Response(503)), budget=0.8, rpc_cfg=cfg(max_retries=5))
    with pytest.raises(RpcUnavailable):
        call(h)
    assert h.sleeps == [0.5]
    assert len(h.requests) == 2
    assert all(s < 0.8 for s in h.sleeps)


def test_retry_after_larger_than_remaining_raises_rate_limited_without_sleeping():
    h = Harness(one_shot(httpx.Response(429, headers={"Retry-After": "60"})), budget=5.0)
    with pytest.raises(RpcRateLimited) as ei:
        call(h)
    assert ei.value.retry_after == 60.0  # справжня причина зберігається; не «бюджет»
    assert h.sleeps == [] and len(h.requests) == 1


def test_deadline_is_checked_before_every_request_including_retries():
    # сон може «проспати» довше за запрошене: наступна спроба бачить спливлий бюджет
    h = Harness(one_shot(httpx.Response(503)), budget=10.0)

    def oversleep(seconds):
        h.sleeps.append(seconds)
        h.clock.advance(100)

    h.source._sleep = oversleep
    with pytest.raises(RpcTimeout) as ei:
        call(h)
    assert str(ei.value) == "budget"
    assert len(h.requests) == 1


def test_expired_deadline_sends_no_request():
    h = Harness(good, budget=5.0)
    h.clock.advance(5.0)  # рівно на межі — бюджет вичерпано
    s, d = h.source, h.deadline
    calls = [
        lambda: s.get_account_info("A", deadline=d),
        lambda: s.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=d),
        lambda: s.get_transactions(["x"], deadline=d),
        lambda: s.get_token_accounts_by_owner("A", deadline=d),
    ]
    for c in calls:
        with pytest.raises(RpcTimeout) as ei:
            c()
        assert str(ei.value) == "budget"
    assert h.requests == []
    assert h.sleeps == []


def test_deadline_expiring_between_chunks_and_between_program_requests_stops_further_requests():
    def burn(request, h):
        h.clock.advance(100)  # відповідь прийшла, але бюджет за час запиту вичерпано
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": i["id"], "result": None} for i in body])
        return ok({"value": []}, id=body["id"])

    h = Harness(burn, budget=50.0, rpc_cfg=cfg(request_timeout_seconds=1000), max_batch=1)
    with pytest.raises(RpcTimeout):
        h.source.get_transactions(["a", "b", "c"], deadline=h.deadline)
    assert len(h.requests) == 1
    h2 = Harness(burn, budget=50.0, rpc_cfg=cfg(request_timeout_seconds=1000))
    with pytest.raises(RpcTimeout):
        h2.source.get_token_accounts_by_owner("O", deadline=h2.deadline)
    assert len(h2.requests) == 1


class _ZeroTimeoutDeadline:
    """Дедлайн, що ще не спливе, але вже не лишає часу на запит (гонка між expired() і запитом)."""

    def expired(self):
        return False

    def remaining(self):
        return 0.0

    def request_timeout(self, cap):
        return 0.0


def test_expired_flag_alone_blocks_the_request_even_if_request_timeout_looks_fine():
    class D:
        def expired(self):
            return True

        def remaining(self):
            return 5.0

        def request_timeout(self, cap):
            return 5.0

    h = Harness(good)
    with pytest.raises(RpcTimeout) as ei:
        h.source.get_account_info("A", deadline=D())
    assert str(ei.value) == "budget"
    assert h.requests == []


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan")])
def test_nonpositive_request_timeout_sends_no_request(value):
    class D(_ZeroTimeoutDeadline):
        def request_timeout(self, cap):
            return value

    h = Harness(good)
    with pytest.raises(RpcTimeout) as ei:
        h.source.get_account_info("A", deadline=D())
    assert str(ei.value) == "budget"
    assert h.requests == []


def test_all_four_httpx_timeouts_equal_request_timeout_capped_by_remaining():
    seen = []

    def handler(request, h):
        seen.append(dict(request.extensions["timeout"]))
        return good(request)

    h = Harness(handler, budget=3.0)  # залишок 3 < cap 10
    call(h)
    h.clock.advance(1.0)  # залишок 2
    call(h)
    h2 = Harness(handler, budget=100.0)  # залишок 100 > cap 10
    call(h2)
    assert seen[0] == {"connect": 3.0, "read": 3.0, "write": 3.0, "pool": 3.0}
    assert seen[1] == {"connect": 2.0, "read": 2.0, "write": 2.0, "pool": 2.0}
    assert seen[2] == {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}


def test_total_request_duration_over_timeout_is_a_timeout_even_if_a_response_arrived():
    # per-read таймаути httpx не обмежують ЗАГАЛЬНУ тривалість (ревʼю T-015): адаптер міряє сам.
    def slow(request, h):
        h.clock.advance(10.5)  # довше за request_timeout_seconds=10
        return good(request)

    h = Harness(slow)
    with pytest.raises(RpcTimeout):
        call(h)
    assert len(h.requests) == 1  # таймаут не повторюється


class _DripStream(httpx.SyncByteStream):
    """Тіло, що «капає» по шматку й просуває годинник: кожен окремий read швидкий, разом — повільно."""

    def __init__(self, clock, chunks, per_chunk):
        self.clock, self.chunks, self.per_chunk = clock, chunks, per_chunk
        self.delivered = 0

    def __iter__(self):
        for c in self.chunks:
            self.clock.advance(self.per_chunk)
            self.delivered += 1
            yield c


def test_slow_drip_body_is_cut_off_by_total_timeout():
    holder = {}

    def drip(request, h):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": None}}).encode()
        stream = _DripStream(h.clock, [body[i:i + 4] for i in range(0, len(body), 4)], 4.0)
        holder["stream"] = stream
        return httpx.Response(200, stream=stream)

    h = Harness(drip)
    with pytest.raises(RpcTimeout):
        call(h)
    total_chunks = len(holder["stream"].chunks)
    assert holder["stream"].delivered < total_chunks  # обірвали, не дочитуючи


# --------------------------------------------------------------------------- секрети


def _all_failure_handlers():
    echo = f"see {URL} key={SECRET}"
    return {
        "429": one_shot(httpx.Response(429, headers={"Retry-After": "1"}, text=echo)),
        "500": one_shot(httpx.Response(500, text=echo)),
        "jsonrpc_error_echoing_url": one_shot(httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": echo}})),
        "jsonrpc_rate_limit_echoing_url": one_shot(httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": 429, "message": echo}})),
        "bad_json": one_shot(httpx.Response(200, text=echo)),
        "bad_shape": one_shot(httpx.Response(200, json={"echo": echo})),
        "connect_error_with_url": one_shot(httpx.ConnectError(f"cannot connect to {URL}")),
        "timeout_with_url": one_shot(httpx.ReadTimeout(f"timed out {URL}")),
        "redirect": one_shot(httpx.Response(302, headers={"Location": URL})),
    }


@pytest.mark.parametrize("case", list(_all_failure_handlers()))
def test_url_with_key_never_appears_in_exceptions_detail_traceback_or_logs(case, caplog):
    handler = _all_failure_handlers()[case]
    h = Harness(handler, rpc_cfg=cfg(max_retries=1, retry_backoff_seconds=0.0))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RpcError) as ei:
            call(h)
    exc = ei.value
    rendered = "".join(traceback.format_exception(exc))
    assert SECRET not in str(exc)
    assert SECRET not in repr(exc)
    assert SECRET not in getattr(exc, "detail", "")
    assert SECRET not in rendered
    assert "rpc.example" not in getattr(exc, "detail", "")
    assert SECRET not in caplog.text
    assert SECRET not in repr(h.source) and SECRET not in str(h.source)


def test_successful_requests_do_not_log_the_url(caplog):
    # httpx на INFO пише «HTTP Request: POST <url>» — із ключем; адаптер це глушить
    h = Harness(good)
    with caplog.at_level(logging.DEBUG):
        call(h)
        h.source.get_transactions([], deadline=h.deadline)
    assert SECRET not in caplog.text
    assert SECRET not in repr(h.source)
    assert h.source.name == "http"


def test_repr_hides_url_but_is_informative():
    h = Harness(good)
    assert "HttpRpcSource" in repr(h.source)
    assert "rpc.example" not in repr(h.source)


def test_from_env_reads_unmask_rpc_url_only_here(monkeypatch):
    seen = []

    def handler(request, h):
        seen.append(str(request.url))
        return good(request)

    src = HttpRpcSource.from_env(
        CFG, commitment="confirmed", transport=httpx.MockTransport(lambda r: handler(r, None)),
        environ={"UNMASK_RPC_URL": URL},
    )
    src.get_account_info("A", deadline=Deadline(FakeClock(), 10))
    assert seen == [URL]
    assert SECRET not in repr(src)
    with pytest.raises(ConfigError) as ei:
        HttpRpcSource.from_env(CFG, commitment="finalized", environ={})
    assert "UNMASK_RPC_URL" in str(ei.value)


# --------------------------------------------------------------------------- ієрархія і сокети


@pytest.mark.parametrize("case", list(_all_failure_handlers()))
def test_adapter_raises_only_rpc_error_hierarchy(case):
    h = Harness(_all_failure_handlers()[case], rpc_cfg=cfg(max_retries=0))
    s, d = h.source, h.deadline
    for c in (
        lambda: s.get_account_info("A", deadline=d),
        lambda: s.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=d),
        lambda: s.get_token_accounts_by_owner("A", deadline=d),
    ):
        with pytest.raises(RpcError) as ei:
            c()
        assert type(ei.value) in (RpcRateLimited, RpcTimeout, RpcUnavailable)


def test_no_socket_opened(monkeypatch):
    import socket

    opened = []
    real = socket.socket.__init__

    def counting_init(self, *a, **kw):
        opened.append(a)
        real(self, *a, **kw)

    monkeypatch.setattr(socket.socket, "__init__", counting_init)
    h = Harness(good)
    call(h)
    h.source.get_transactions([], deadline=h.deadline)
    assert opened == []  # MockTransport не відкриває сокетів


def test_default_transport_is_blocked_by_the_conftest_guard():
    # без transport= адаптер ходив би у справжню мережу; гард conftest це зупиняє на connect()
    # (127.0.0.1: без DNS). Доводить, що гард активний і для httpx-шляху.
    src = HttpRpcSource("http://127.0.0.1:9/", cfg(max_retries=0), commitment="finalized")
    with pytest.raises(Exception) as ei:
        src.get_account_info("A", deadline=Deadline(FakeClock(), 10))
    assert type(ei.value).__name__ == "NetworkForbidden"


# --------------------------------------------------------------------------- очищення повідомлень провайдера


def _variants(secret: str) -> set[str]:
    return {secret, quote(secret, safe=""), quote_plus(secret), secret.replace("+", " ")}


def assert_clean(exc: BaseException, secrets: list[str]) -> None:
    """Жодного секрету (у жодному вигляді й регістрі) в str/repr/detail/args/traceback винятку."""
    parts = [str(exc), repr(exc), getattr(exc, "detail", ""), *map(str, exc.args),
             "".join(traceback.format_exception(exc))]
    haystack = "\n".join(parts).lower()
    for secret in secrets:
        for v in _variants(secret):
            assert v.lower() not in haystack, f"leaked {v!r}: {haystack}"


def jsonrpc_error_response(message, *, code=-32001, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": err})


SCRUB_CASES = {
    # (url, секретні фрагменти, що провайдер може повернути у message)
    "base64_plus_slash_eq": (
        "https://rpc.example/?api-key=Ab+Cd/Ef==", ["Ab+Cd/Ef=="],
        ["Invalid api-key Ab+Cd/Ef==", "Invalid api-key Ab Cd/Ef==", "Invalid api-key Ab%2BCd%2FEf%3D%3D",
         "Invalid api-key ab%2bcd%2fef%3d%3d", "Invalid api-key Ab+Cd/Ef"]),
    "percent_encoded_raw": (
        "https://rpc.example/?api-key=ab%2Fcd%2Bef99", ["ab%2Fcd%2Bef99", "ab/cd+ef99"],
        ["bad key ab%2Fcd%2Bef99", "bad key ab/cd+ef99", "bad key AB%2fCD%2bEF99", "bad key ab cd ef99"]),
    "lowercase_echo": (
        f"https://rpc.example/?api-key={SECRET}&k=KEY123", [SECRET, "KEY123"],
        ["bad key secret123", "bad key key123", "BAD KEY Secret123/kEy123"]),
    "subdomain_key": (
        "https://SUBKEY777.rpc.example/", ["SUBKEY777"],
        ["cannot reach subkey777.rpc.example", "host SUBKEY777.rpc.example refused",
         "unknown tenant SUBKEY777", "unknown tenant subkey777"]),
    "path_key": (
        "https://rpc.example/v2/PathKey99999", ["PathKey99999"],
        ["no such route /v2/PathKey99999", "pathkey99999 unknown"]),
    "userinfo": (
        "https://user:pa55word@rpc.example/", ["pa55word"], ["auth failed for user:pa55word", "PA55WORD"]),
    "full_url": (
        URL, [SECRET], [f"POST {URL} failed", f"POST {quote(URL, safe='')} failed", f"POST {URL.lower()} failed"]),
    "substring_of_word": (
        URL, [SECRET], ["xxSECRET123yy", "prefix-secret123-suffix"]),
}


@pytest.mark.parametrize("name", list(SCRUB_CASES))
def test_provider_messages_are_scrubbed_of_every_form_of_the_url_secrets(name):
    url, secrets, messages = SCRUB_CASES[name]
    for message in messages:
        h = Harness(one_shot(jsonrpc_error_response(message)), rpc_cfg=cfg(max_retries=0), url=url)
        with pytest.raises(RpcUnavailable) as ei:
            call(h)
        assert_clean(ei.value, secrets)
        assert ei.value.detail == "jsonrpc error code=-32001 (resource not found)"  # лише код і фіксована мітка


@pytest.mark.parametrize("name", list(SCRUB_CASES))
def test_rate_limited_and_batch_error_paths_do_not_leak_either(name):
    url, secrets, messages = SCRUB_CASES[name]
    for message in messages:
        results = {"s1": {"error": {"code": -32602, "message": message}}}
        h = Harness(batch_handler(results), rpc_cfg=cfg(max_retries=0), url=url)
        with pytest.raises(RpcUnavailable) as ei:
            h.source.get_transactions(["s1"], deadline=h.deadline)
        assert_clean(ei.value, secrets)
        h = Harness(one_shot(jsonrpc_error_response(message, code=429)), rpc_cfg=cfg(max_retries=0), url=url)
        with pytest.raises(RpcRateLimited) as ei:
            call(h)
        assert_clean(ei.value, secrets)


def test_error_data_and_non_json_body_with_the_key_never_reach_the_exception():
    h = Harness(one_shot(jsonrpc_error_response("boom", data={"url": URL, "key": SECRET})),
                rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert_clean(ei.value, [SECRET])
    assert "boom" not in ei.value.detail and ei.value.detail == "jsonrpc error code=-32001 (resource not found)"
    h = Harness(one_shot(httpx.Response(200, text=f"<html>{URL} {SECRET}")), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert_clean(ei.value, [SECRET])


def test_diagnostics_come_from_the_fixed_table_and_perverted_key_never_survives():
    # Раніше: «корисний текст провайдера лишається». Тепер діагностика — лише мітка з таблиці адаптера.
    h = Harness(one_shot(jsonrpc_error_response("Method getFoo is not here", code=-32601)),
                rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert ei.value.detail == "jsonrpc error code=-32601 (method not found)"
    # провайдер перекрутив ключ: неважливо як — текст провайдера не потрапляє взагалі
    h = Harness(one_shot(jsonrpc_error_response("bad key SECRET123-rotated-by-provider-xyz")),
                rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert "SECRET123" not in ei.value.detail and "rotated" not in ei.value.detail
    assert ei.value.detail == "jsonrpc error code=-32001 (resource not found)"


# --------------------------------------------------------------------------- ланцюг винятків


def _raising(factory):
    def handler(request, h):
        raise factory(request)

    return handler


CHAIN_CASES = {
    "connect_error": _raising(lambda r: httpx.ConnectError(f"cannot connect {r.url}", request=r)),
    "read_error": _raising(lambda r: httpx.ReadError(f"reset {r.url}", request=r)),
    "remote_protocol": _raising(lambda r: httpx.RemoteProtocolError(f"bad {r.url}", request=r)),
    "decoding_error": _raising(lambda r: httpx.DecodingError(f"gzip {r.url}", request=r)),
    "read_timeout": _raising(lambda r: httpx.ReadTimeout(f"timeout {r.url}", request=r)),
    "connect_timeout": _raising(lambda r: httpx.ConnectTimeout(f"timeout {r.url}", request=r)),
    "invalid_json": one_shot(httpx.Response(200, text=f"{{{URL} {SECRET}")),
    "deep_json": one_shot(httpx.Response(200, content=b"[" * 200000 + SECRET.encode() + b"]" * 200000)),
    "bad_shape": one_shot(httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": SECRET})),
    "429": one_shot(httpx.Response(429, text=URL)),
    "500": one_shot(httpx.Response(500, text=URL)),
    "jsonrpc_error": one_shot(jsonrpc_error_response(f"{URL} {SECRET}")),
}


def _walk_chain(exc):
    seen = []
    e = exc
    while e is not None:
        seen.append(e)
        e = e.__cause__ or e.__context__
    return seen


@pytest.mark.parametrize("case", list(CHAIN_CASES))
@pytest.mark.parametrize("retries", [0, 1])
def test_exception_chain_is_empty_and_nothing_in_it_holds_the_key(case, retries):
    h = Harness(CHAIN_CASES[case], rpc_cfg=cfg(max_retries=retries, retry_backoff_seconds=0.0))
    with pytest.raises(RpcError) as ei:
        call(h)
    exc = ei.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    for e in _walk_chain(exc):
        texts = [str(e), repr(e), *map(str, e.args), *(repr(v) for v in vars(e).values())]
        assert SECRET not in "\n".join(texts), f"{type(e).__name__}: {texts}"
    assert_clean(exc, [SECRET])


def test_exception_chain_is_empty_for_batch_and_signatures_paths_too():
    h = Harness(CHAIN_CASES["connect_error"], rpc_cfg=cfg(max_retries=0))
    for c in (
        lambda: h.source.get_transactions(["s1"], deadline=h.deadline),
        lambda: h.source.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=h.deadline),
        lambda: h.source.get_token_accounts_by_owner("A", deadline=h.deadline),
    ):
        with pytest.raises(RpcUnavailable) as ei:
            c()
        assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_deeply_nested_json_is_unavailable_not_a_recursion_error():
    body = b"[" * 200000 + b"]" * 200000
    h = Harness(one_shot(httpx.Response(200, content=body)), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert ei.value.detail == "invalid JSON in response"


# --------------------------------------------------------------------------- логи httpcore


def test_httpcore_debug_host_records_are_dropped(caplog):
    # ключ може бути в піддомені хоста: httpcore на DEBUG пише `connect_tcp.started host=...`
    HttpRpcSource("https://SUBKEY777.rpc.example/", CFG, commitment="finalized")  # імпорт/фільтр активні
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpcore.connection").debug("connect_tcp.started host='SUBKEY777.rpc.example' port=443")
        logging.getLogger("httpcore").debug("start_tls.started server_hostname='SUBKEY777.rpc.example'")
        logging.getLogger("httpcore").debug("send_request_headers.started request=<Request [b'POST']>")
    assert "SUBKEY777" not in caplog.text.upper()
    # Політика (ескалація T-021): логери httpcore відкидають УСІ записи — транспортна діагностика втрачається.
    assert "send_request_headers" not in caplog.text
    assert [r for r in caplog.records if r.name.startswith("httpcore")] == []


def test_real_httpcore_does_not_log_the_host_when_the_guard_stops_the_connection(caplog):
    src = HttpRpcSource("http://127.0.0.1:9/", cfg(max_retries=0), commitment="finalized")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as ei:
            src.get_account_info("A", deadline=Deadline(FakeClock(), 10))
    assert type(ei.value).__name__ == "NetworkForbidden"
    assert "127.0.0.1" not in caplog.text


# --------------------------------------------------------------------------- сумісність із ядром (постійний тест)

SCENARIOS = Path(__file__).parent / "fixtures" / "scenarios"
_VOLATILE = ("analyzed_at", "elapsed_seconds", "rpc_calls", "resumed", "served_from_cache", "source")


# Ліміти живого провайдера (known-issues §6, T-049, ревʼю): кошик токенів на getTransaction-елементи
# (`ProviderBucket`: ємність 40, 15.5/с; за нестачі — HTTP 429 з тілом -32005); транзакція `version: 1` при
# `maxSupportedTransactionVersion` < 1 — елемент з -32015.
REAL_CORPUS = Path(__file__).parent / "fixtures" / "real" / "mainnet_txs_2026-10-04.json"


def _real_v1_transaction() -> tuple[str, dict]:
    """Перша транзакція `version: 1` з реального корпусу (підпис, getTransaction.result)."""
    corpus = json.loads(REAL_CORPUS.read_text())["transactions"]
    sig, tx = next((sig, tx) for sig, tx in corpus.items() if tx.get("version") == 1)
    return sig, tx


def _emulator(rpc: dict, commitment: str, bucket: "ProviderBucket", *, shared: bool = False):
    """JSON-RPC сервер поверх записаного rpc.json; batch відповідає у ЗВОРОТНОМУ порядку.

    Поводиться як живий провайдер: batch getTransaction на k елементів списує k токенів кошика `bucket`,
    за нестачі (зокрема k > ємності) — HTTP 429 з тілом -32005 без Retry-After; getTransaction транзакції з
    `version` > maxSupportedTransactionVersion -> елемент з помилкою -32015 (`legacy`/без версії — завжди).
    `shared=True` — модель Helius free (T-053): ОДИН кошик на всі методи, одиночний запит списує 1 токен."""

    def answer(req):
        method, params = req["method"], req["params"]
        assert params[-1]["commitment"] == commitment
        if method == "getAccountInfo":
            assert params[1]["encoding"] == "jsonParsed"
            return {"context": {"slot": 1}, "value": copy.deepcopy(rpc["getAccountInfo"].get(params[0]))}
        if method == "getSignaturesForAddress":
            opts = params[1]
            history = rpc["getSignaturesForAddress"].get(params[0]) or []
            order = [e["signature"] for e in history]
            start = order.index(opts["before"]) + 1 if opts.get("before") else 0
            stop = order.index(opts["until"]) if opts.get("until") else len(history)
            return copy.deepcopy(history[start:stop][: opts["limit"]])
        if method == "getTransaction":
            opts = params[1]
            assert set(opts) == {"encoding", "maxSupportedTransactionVersion", "commitment"}
            assert opts["encoding"] == "jsonParsed"
            tx = rpc["getTransaction"].get(params[0])
            version = tx.get("version") if isinstance(tx, dict) else None
            if type(version) is int and version > opts["maxSupportedTransactionVersion"]:
                return _Error(-32015, f"Transaction version ({version}) is not supported by the requesting "
                                      f"client. Please try the request again with the following configuration "
                                      f"parameter: \"maxSupportedTransactionVersion\": {version}")
            return copy.deepcopy(tx)
        if method == "getTokenAccountsByOwner":
            program = params[1]["programId"]
            listed = rpc["getTokenAccountsByOwner"].get(params[0], [])
            chosen = [e for e in listed if (e.get("account", {}).get("owner") or TOKEN_PROGRAM) == program]
            return {"context": {"slot": 1}, "value": copy.deepcopy(chosen)}
        raise AssertionError(method)

    def entry(req):
        result = answer(req)
        if isinstance(result, _Error):
            return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": result.code, "message": result.message}}
        return {"jsonrpc": "2.0", "id": req["id"], "result": result}

    def handler(request):
        body = json.loads(request.content)
        if isinstance(body, list):
            if not bucket.take(len(body)):
                return httpx.Response(429, json=RATE_LIMITED_429)
            return httpx.Response(200, json=[entry(x) for x in body][::-1])
        if shared and not bucket.take(1):
            return httpx.Response(429, json=RATE_LIMITED_429)
        return httpx.Response(200, json=entry(body))

    return handler


@dataclasses.dataclass(frozen=True)
class _Error:
    code: int
    message: str


def _stable(result) -> dict:
    d = to_dict(result)
    for key in _VOLATILE:
        d.get("metadata", {}).pop(key, None)
    return d


def _scenario_with_v1_transaction(scenario: str, tmp_path: Path) -> Path:
    """Копія сценарію, де одна транзакція, яку ядро справді запитує, має `version: 1` (як у корпусі)."""
    target = tmp_path / scenario
    target.mkdir()
    for f in (SCENARIOS / scenario).iterdir():
        (target / f.name).write_bytes(f.read_bytes())
    rpc = json.loads((target / "rpc.json").read_text())
    _, real = _real_v1_transaction()
    sig = next(s for s, tx in rpc["getTransaction"].items() if isinstance(tx, dict))
    rpc["getTransaction"][sig]["version"] = real["version"]
    (target / "rpc.json").write_text(json.dumps(rpc))
    return target


@pytest.mark.parametrize("scenario, with_v1", [
    ("basic", True),
    ("hub", False),  # історія 1200 підписів: ядро шле пачки до rpc.page_size=1000
    ("hub", True),
])
def test_ingest_service_over_http_source_equals_fixture_source(scenario, with_v1, tmp_path):
    # Емулятор поводиться як живий RPC: кошик токенів на getTransaction-елементи (ємність 40, 15.5/с) і
    # відмова -32015 на maxSupportedTransactionVersion < version. Без T-049 (batch = rpc.page_size, версія 0,
    # без пейсера) тест дає 429 / `incomplete` — він доводить виправлення.
    directory = _scenario_with_v1_transaction(scenario, tmp_path) if with_v1 else SCENARIOS / scenario
    rpc = json.loads((directory / "rpc.json").read_text())
    real_sig, real_tx = _real_v1_transaction()
    rpc["getTransaction"][real_sig] = copy.deepcopy(real_tx)  # реальна v1-транзакція корпусу на «сервері»
    expected = json.loads((directory / "expected.json").read_text())
    config = load_config(Path(__file__).parent.parent / "config" / "ingest.yaml")
    if "config" in expected:
        config = dataclasses.replace(config, **expected["config"])
    mint = expected["mint"]

    from_fixture = IngestService(config, FixtureRpcSource(directory), clock=FakeClock()).collect(mint)
    # Годинник адаптера й провайдера спільний (пауза пейсера поповнює кошик провайдера); дедлайн сервісу —
    # окремий FakeClock: тест про сумісність і ліміт швидкості, а не про бюджет часу.
    source_clock = FakeClock()
    bucket = ProviderBucket(source_clock)
    seen_batches: list[int] = []
    emulator = _emulator(rpc, config.commitment, bucket)

    def recording(request):
        body = json.loads(request.content)
        if isinstance(body, list):
            seen_batches.append(len(body))
        return emulator(request)

    http_source = HttpRpcSource(
        URL, config.rpc, transport=httpx.MockTransport(recording),
        commitment=config.commitment, clock=source_clock, sleep=source_clock.advance,
    )
    from_http = IngestService(config, http_source, clock=FakeClock()).collect(mint)

    assert to_dict(from_http)["metadata"]["source"] == "http"
    assert _stable(from_http) == _stable(from_fixture)
    assert _stable(from_http)["completeness"]["status"] == "complete"
    assert bucket.rejected == []  # жодного 429: пейсер не випереджає провайдера
    assert seen_batches and max(seen_batches) <= 25  # max_batch за замовчуванням
    if scenario == "hub":
        assert max(seen_batches) == 25 and len(seen_batches) > 2  # пачку ядра справді розбито й розтягнуто в часі
        assert source_clock.monotonic() >= (sum(seen_batches) - 30) / 12.0 - 1e-9  # 305 елементів: ≈ 22.9 с
    # реальна v1-транзакція корпусу проходить крізь адаптер без змін
    assert http_source.get_transactions([real_sig], deadline=Deadline(FakeClock(), 10)) == [real_tx]


def test_emulator_really_rejects_what_the_live_provider_rejects():
    # Самоперевірка емулятора: інакше зелений тест сумісності нічого б не доводив.
    real_sig, real_tx = _real_v1_transaction()
    rpc = {"getTransaction": {real_sig: real_tx, **{f"x{i}": None for i in range(41)}}}

    def harness(**kw):
        holder = {}
        h = Harness(lambda request, h: _emulator(rpc, "finalized", holder["bucket"])(request),
                    rpc_cfg=cfg(max_retries=0), **kw)
        holder["bucket"] = ProviderBucket(h.clock)
        return h

    h = harness(max_tx_version=0)
    with pytest.raises(RpcUnavailable) as ei:
        h.source.get_transactions([real_sig], deadline=h.deadline)
    assert ei.value.detail == "batch item 0: jsonrpc error code=-32015 (unsupported transaction version)"
    h = harness(max_batch=41, burst=41)  # більше за ємність кошика провайдера — ніколи не пройде
    with pytest.raises(RpcRateLimited):
        h.source.get_transactions([f"x{i}" for i in range(41)], deadline=h.deadline)
    h = harness(max_batch=40, burst=40)
    assert h.source.get_transactions([f"x{i}" for i in range(40)], deadline=h.deadline) == [None] * 40
    h = harness(rate_per_second=math.inf)  # 25 і одразу ще 25 -> другий 429 (як на живому вузлі)
    with pytest.raises(RpcRateLimited):
        h.source.get_transactions([f"x{i}" for i in range(41)] + [f"x{i}" for i in range(9)], deadline=h.deadline)
    assert len(h.requests) == 2


# --------------------------------------------------------------------------- ескалація T-021: політика allow-list
#
# Рішення власника процесу: `detail` і тексти винятків НІКОЛИ не містять вільного тексту провайдера
# (ні `error.message`, ні `error.data`, ні тіла, ні заголовків) — лише категорію з фіксованої таблиці
# адаптера; числовий код друкується лише як справжній `int` у межах int32. Очищувач тексту прибрано:
# кожне нове кодування ключа обходило його, а вільний текст, що не потрапляє, нічого не несе.

CANARY = "PROVIDERCANARY"


def _err_body(error) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": error})


DETAIL_CASES = {
    "http_401": (httpx.Response(401, text=f"Invalid api key {CANARY}", headers={"X-Debug": CANARY}),
                 "http 401 unauthorized (check API key)"),
    "http_403": (httpx.Response(403, text=CANARY), "http 403 unauthorized (check API key)"),
    "http_404": (httpx.Response(404, text=CANARY), "http 404 not found"),
    "http_500": (httpx.Response(500, text=CANARY), "http 500 server error"),
    "http_503": (httpx.Response(503, text=CANARY), "http 503 server error"),
    "http_301": (httpx.Response(301, headers={"Location": f"https://x.example/{CANARY}"}),
                 "http 301 redirect (not followed)"),
    "http_418": (httpx.Response(418, text=CANARY), "http 418"),
    "http_199": (httpx.Response(199, text=CANARY), "http 199"),
    "known_code_with_message_and_data": (
        _err_body({"code": -32602, "message": CANARY, "data": {"why": CANARY}}),
        "jsonrpc error code=-32602 (invalid params)"),
    "unknown_code": (_err_body({"code": -31999, "message": CANARY}), "jsonrpc error code=-31999"),
    "positive_unknown_code": (_err_body({"code": 7, "message": CANARY}), "jsonrpc error code=7"),
    "string_code": (_err_body({"code": CANARY, "message": CANARY}), "jsonrpc error (malformed code)"),
    "bool_code": (_err_body({"code": True, "message": CANARY}), "jsonrpc error (malformed code)"),
    "float_code": (_err_body({"code": -32001.0, "message": CANARY}), "jsonrpc error (malformed code)"),
    "huge_code": (_err_body({"code": 12345678901234567890, "message": CANARY}), "jsonrpc error (malformed code)"),
    "no_code": (_err_body({"message": CANARY}), "jsonrpc error (malformed code)"),
    "error_not_object": (_err_body(CANARY), "jsonrpc error (malformed)"),
    "invalid_json": (httpx.Response(200, text=f"<html>{CANARY}"), "invalid JSON in response"),
}


@pytest.mark.parametrize("case", list(DETAIL_CASES))
def test_detail_is_a_category_from_the_allow_list_never_provider_text(case):
    response, expected = DETAIL_CASES[case]
    h = Harness(one_shot(response), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert ei.value.detail == expected
    assert ei.value.args == (expected,)
    assert CANARY not in "".join(traceback.format_exception(ei.value))


def test_batch_item_error_detail_is_position_plus_allow_listed_category():
    results = {"s1": txr(1), "s2": {"error": {"code": -32011, "message": CANARY, "data": CANARY}}}
    h = Harness(batch_handler(results), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        h.source.get_transactions(["s1", "s2"], deadline=h.deadline)
    assert ei.value.detail == "batch item 1: jsonrpc error code=-32011 (transaction history not available)"


def test_batch_rejected_as_a_whole_uses_the_same_allow_list():
    h = Harness(batch_handler({"s1": txr(1)}, as_dict={"jsonrpc": "2.0", "id": None,
                                                      "error": {"code": -32600, "message": CANARY}}),
                rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        h.source.get_transactions(["s1"], deadline=h.deadline)
    assert ei.value.detail == "jsonrpc error code=-32600 (invalid request)"


class SECRETKEY99ConnectError(httpx.ConnectError):
    """Транспорт може підняти підклас із будь-якою назвою — назва класу теж «чужий текст»."""


class SECRETKEY99TransportError(httpx.TransportError):
    pass


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectError("x"), "network error: ConnectError"),
        (httpx.ReadError("x"), "network error: ReadError"),
        (httpx.WriteError("x"), "network error: WriteError"),
        (httpx.RemoteProtocolError("x"), "network error: RemoteProtocolError"),
        (httpx.LocalProtocolError("x"), "network error: LocalProtocolError"),
        (httpx.ProxyError("x"), "network error: ProxyError"),
        (httpx.UnsupportedProtocol("x"), "network error: UnsupportedProtocol"),
        (httpx.DecodingError("x"), "network error: DecodingError"),
        (SECRETKEY99ConnectError("x"), "network error: ConnectError"),
        (SECRETKEY99TransportError("x"), "network error"),
    ],
)
def test_network_error_detail_is_a_fixed_label_not_the_runtime_class_name(exc, expected):
    h = Harness(one_shot(exc), rpc_cfg=cfg(max_retries=0))
    with pytest.raises(RpcUnavailable) as ei:
        call(h)
    assert ei.value.detail == expected


# --------------------------------------------------------------------------- ескалація T-021: валідація URL


LEAKY_URLS = [
    "https://apikey:SECRETKEY99/",  # ключ на місці порту
    "https://rpc.example:SECRETKEY99/",
    "https://rpc.example:443:SECRETKEY99/",
    "https://[SECRETKEY99]/",  # urlsplit сам кидає ValueError із ключем
]
OTHER_BAD_URLS = [
    "ftp://rpc.example/?k=SECRETKEY99",
    "",
    "SECRETKEY99",
    "https:///SECRETKEY99",
    "http://:80/SECRETKEY99",
    "https://rpc.example:0/SECRETKEY99",
    "https://rpc.example:99999/SECRETKEY99",
    "https://rpc.example/\x00SECRETKEY99",
    "https://rpc .example/SECRETKEY99",
    "https://SECRETKEY99\u200d.example/",  # невалідний IDNA: urlsplit приймає, httpx кидає InvalidURL з хостом
    "https://1.2.3.999/SECRETKEY99",  # невалідний IPv4: так само
    b"https://rpc.example/SECRETKEY99",
    None,
]


def _assert_generic_invalid_url(exc: BaseException) -> None:
    assert type(exc) is ValueError
    assert str(exc) == "invalid RPC URL" and exc.args == ("invalid RPC URL",)
    assert exc.__cause__ is None and exc.__context__ is None
    assert exc.__suppress_context__ is False
    assert "SECRETKEY99" not in "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("url", LEAKY_URLS + OTHER_BAD_URLS)
def test_invalid_url_is_rejected_in_constructor_with_one_generic_message(url):
    requests = []
    transport = httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200))
    with pytest.raises(ValueError) as ei:
        HttpRpcSource(url, CFG, transport=transport, commitment="finalized")
    _assert_generic_invalid_url(ei.value)
    assert requests == []


@pytest.mark.parametrize("url", LEAKY_URLS + OTHER_BAD_URLS[:-2])
def test_from_env_with_invalid_url_is_config_error_without_the_url(url):
    with pytest.raises(ConfigError) as ei:
        HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": url} if url else {})
    exc = ei.value
    assert "UNMASK_RPC_URL" in str(exc)
    assert exc.__cause__ is None and exc.__context__ is None
    assert "SECRETKEY99" not in "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("url", [
    "https://rpc.example/?api-key=SECRET123",
    "https://user:pw@rpc.example:8899/v2/KEY",
    "http://127.0.0.1:8899",
    "https://SUBKEY777.rpc.example/",
    "https://rpc.example/?BareKey123",
    "https://rpc.example/?api-key=aB3+dE6/gH9+jK2/mN5+pQ8/sT1+vW4=",
])
def test_valid_urls_are_accepted_and_used_verbatim(url):
    seen = []

    def handler(request, h):
        seen.append(request.url)
        return good(request)

    h = Harness(handler, url=url)
    call(h)
    assert seen == [httpx.URL(url)]


# --------------------------------------------------------------------------- ескалація T-021: логери httpx/httpcore

LOG_KEY = "Zq8kLongSecretKey77"


def _real_httpcore_transport(*raw_responses: bytes) -> httpx.HTTPTransport:
    """Справжній httpx.HTTPTransport + справжній httpcore, але мережа — httpcore.MockBackend (без сокетів)."""
    transport = httpx.HTTPTransport()
    transport._pool = httpcore.ConnectionPool(network_backend=httpcore.MockBackend(list(raw_responses)))
    return transport


def _raw(status_line: bytes, headers: list[tuple[bytes, bytes]], body: bytes = b"") -> bytes:
    head = b"".join(k + b": " + v + b"\r\n" for k, v in headers)
    return status_line + b"\r\n" + head + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


@pytest.mark.parametrize("raw, expected", [
    (_raw(b"HTTP/1.1 301 Moved", [(b"Location", f"https://rpc.example/?api-key={LOG_KEY}".encode()),
                                   (b"X-Debug", f"key={LOG_KEY}".encode())]), RpcUnavailable),
    (_raw(b"HTTP/1.1 200 OK", [(b"X-Debug", f"key={LOG_KEY}".encode()), (b"Content-Type", b"application/json")],
          json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": LOG_KEY}}).encode()),
     RpcUnavailable),
    (_raw(b"HTTP/1.1 200 OK", [(b"X-Debug", f"key={LOG_KEY}".encode())],
          json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": None}}).encode()), None),
])
def test_real_httpcore_never_logs_the_key_from_url_or_response_headers(raw, expected, caplog):
    url = f"https://{LOG_KEY}.rpc.example/?api-key={LOG_KEY}"
    src = HttpRpcSource(url, cfg(max_retries=0), _real_httpcore_transport(raw), commitment="finalized",
                        clock=FakeClock())
    with caplog.at_level(1):
        if expected is None:
            assert src.get_account_info("A", deadline=Deadline(FakeClock(), 10)) is None
        else:
            with pytest.raises(expected) as ei:
                src.get_account_info("A", deadline=Deadline(FakeClock(), 10))
            assert LOG_KEY not in "".join(traceback.format_exception(ei.value))
    text = "\n".join(f"{r.name} {r.getMessage()} {r.args!r}" for r in caplog.records)
    assert LOG_KEY.lower() not in text.lower()
    assert [r.name for r in caplog.records if r.name.split(".")[0] in ("httpx", "httpcore")] == []


def _installed_logger_names(package) -> set[str]:
    root = Path(package.__file__).parent
    names = set()
    for f in root.rglob("*.py"):
        names.update(re.findall(r"getLogger\(\s*[\"']([^\"']+)[\"']", f.read_text()))
    return names


@pytest.mark.parametrize("package", [httpx, httpcore])
def test_every_logger_of_the_installed_transport_packages_is_silenced(package, caplog):
    import unmask.ingest.rpc.http  # noqa: F401  — фільтри ставляться при імпорті

    names = _installed_logger_names(package) | {package.__name__}
    assert names  # регекс знаходить логери встановленої версії
    with caplog.at_level(1):
        for name in sorted(names):
            for level in (1, logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
                logging.getLogger(name).log(level, "probe %s", f"https://{LOG_KEY}.example/")
    assert [r.name for r in caplog.records] == []


# --------------------------------------------------------------------------- ескалація T-021: «ключ посередині»

B64_KEY = "aB3+dE6/gH9+jK2/mN5+pQ8/sT1+vW4="
BARE_KEY = "BareKey123LongEnoughXyz"


def _html_entities(s: str) -> str:
    return s.replace("+", "&#43;").replace("/", "&#47;").replace("=", "&#61;")


def _encodings(key: str) -> set[str]:
    once = quote(key, safe="")
    return {
        key, key.lower(), key[:8], key[-8:], once, once.lower(), quote(once, safe=""), quote_plus(key),
        key.replace("/", "\\/"), _html_entities(key), key.replace("+", " "),
        key.replace("+", "&#43;"), key[:12],
    }


# Як провайдер повертає ключ: (url, ключ, сирий текст відповіді-помилки, заголовки)
def _raw_error(message_json_literal: str) -> str:
    return '{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"' + message_json_literal + \
        '","data":{"echo":"' + message_json_literal + '"}}}'


MIDDLE_URL = f"https://rpc.example/?api-key={B64_KEY}"
MIDDLE_CASES = {
    "json_escaped_slash": (MIDDLE_URL, B64_KEY, _raw_error("invalid api-key " + B64_KEY.replace("/", "\\\\/"))),
    "double_encoded": (MIDDLE_URL, B64_KEY, _raw_error("invalid " + quote(quote(B64_KEY, safe=""), safe=""))),
    "html_entities": (MIDDLE_URL, B64_KEY, _raw_error("invalid " + _html_entities(B64_KEY))),
    "prefix_mask": (MIDDLE_URL, B64_KEY, _raw_error("invalid key " + B64_KEY[:12] + "********")),
    "bare_query_key": (f"https://rpc.example/?{BARE_KEY}", BARE_KEY, _raw_error("unknown " + BARE_KEY)),
    "key_at_position_200": (MIDDLE_URL, B64_KEY, _raw_error("x" * 200 + B64_KEY)),
    "plain_body_not_json": (MIDDLE_URL, B64_KEY, f"<html>{B64_KEY} {quote(B64_KEY, safe='')}</html>"),
}


def _middle_handler(raw_text: str, key: str):
    def handler(request, h=None):
        return httpx.Response(200, content=raw_text.encode(),
                              headers={"X-Debug": key, "Content-Type": "application/json"})

    return handler


def _assert_no_key_anywhere(haystack: str, key: str) -> None:
    low = haystack.lower()
    for needle in _encodings(key):
        assert needle.lower() not in low, f"leaked {needle!r}"
    assert CANARY not in haystack


def _exception_texts(exc: BaseException) -> str:
    parts = []
    for e in _walk_chain(exc):
        parts += [str(e), repr(e), *map(repr, e.args), repr(getattr(e, "detail", "")),
                  *(repr(v) for v in vars(e).values()), repr(e.__cause__), repr(e.__context__)]
    parts.append("".join(traceback.format_exception(exc)))
    return "\n".join(parts)


@pytest.mark.parametrize("case", list(MIDDLE_CASES))
def test_key_in_the_middle_of_provider_text_never_leaks_from_direct_adapter_calls(case, caplog):
    url, key, raw_text = MIDDLE_CASES[case]
    h = Harness(_middle_handler(raw_text, key), rpc_cfg=cfg(max_retries=1, retry_backoff_seconds=0.0), url=url)
    s, d = h.source, h.deadline
    texts = []
    with caplog.at_level(1), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for c in (
            lambda: s.get_account_info("A", deadline=d),
            lambda: s.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=d),
            lambda: s.get_transactions(["s1"], deadline=d),
            lambda: s.get_token_accounts_by_owner("A", deadline=d),
        ):
            with pytest.raises(RpcUnavailable) as ei:
                c()
            assert ei.value.__cause__ is None and ei.value.__context__ is None
            texts.append(_exception_texts(ei.value))
    texts.append("\n".join(f"{r.name} {r.getMessage()} {r.args!r}" for r in caplog.records))
    texts.append("\n".join(str(w.message) for w in caught))
    texts += [repr(s), str(s)]
    _assert_no_key_anywhere("\n".join(texts), key)


@pytest.mark.parametrize("case", list(MIDDLE_CASES))
def test_key_in_the_middle_never_reaches_the_ingest_result_json(case, caplog):
    from unmask.ingest.serialize import to_json

    url, key, raw_text = MIDDLE_CASES[case]
    directory = SCENARIOS / "basic"
    config = load_config(Path(__file__).parent.parent / "config" / "ingest.yaml")
    mint = json.loads((directory / "expected.json").read_text())["mint"]
    source = HttpRpcSource(url, config.rpc, transport=httpx.MockTransport(_middle_handler(raw_text, key)),
                           commitment=config.commitment, clock=FakeClock(), sleep=lambda s: None)
    with caplog.at_level(1), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = to_json(IngestService(config, source, clock=FakeClock()).collect(mint))
    expected = "invalid JSON in response" if case == "plain_body_not_json" else \
        "jsonrpc error code=-32001 (resource not found)"
    assert expected in out  # причина видна — категорією з allow-list
    logs = "\n".join(f"{r.name} {r.getMessage()} {r.args!r}" for r in caplog.records)
    _assert_no_key_anywhere("\n".join([out, logs, *(str(w.message) for w in caught)]), key)


# --------------------------------------------------------------------------- T-053: глобальний лімітер і профілі
#
# Виміри (known-issues §8, Helius free): ОДИН кошик на всі методи (getSignaturesForAddress, getTokenAccountsByOwner,
# getAccountInfo, getTransaction), кожен елемент batch = одиниця; ємність ≈ 30 (batch 30 проходить, 35 ні),
# поповнення ≈ 5/с (10/12/16 запитів/с протягом 6 с -> прийнято 59–60 = 30 + 5·6; сталі 5/с — без 429).
# Емулятор — ця модель: один кошик на ВСІ запити, ємність 30, 5/с. Профіль helius_free = 4.5/с, кошик 20,
# max_batch 10 (рішення власника процесу після живої перевірки: 0×429 за 80 с); попередні 8/10 — 429.
# RPC Fast «Start» (known-issues §6, T-049): ємність ≈ 40, поповнення ≈ 15–16/с.

import ast  # noqa: E402

import unmask.ingest.rpc.http as http_module  # noqa: E402

HELIUS_CAPACITY = 30
HELIUS_RATE = 5.0


def all_methods_handler(*, tx=None):
    """Відповідає на будь-який метод (одиночний або batch) валідною формою; без лімітів."""

    def handler(request, h):
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": i["id"], "result": tx} for i in body])
        result = {"getAccountInfo": {"context": {"slot": 1}, "value": None},
                  "getSignaturesForAddress": [],
                  "getTokenAccountsByOwner": {"context": {"slot": 1}, "value": []}}[body["method"]]
        return ok(result, id=body["id"])

    return handler


def test_global_limiter_paces_every_method_not_only_get_transaction():
    seen = []
    inner = all_methods_handler()

    def handler(request, h):
        body = json.loads(request.content)
        seen.append((body[0]["method"] if isinstance(body, list) else body["method"], h.source._tokens))
        return inner(request, h)

    h = Harness(handler, rate_per_second=2.0, burst=2, max_batch=2)
    s, d = h.source, h.deadline
    s.get_account_info("A", deadline=d)                     # 2 -> 1, без паузи
    s.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=d)  # 1 -> 0
    s.get_account_info("A", deadline=d)                     # чекати 1/2 с
    s.get_transactions(["a", "b"], deadline=d)              # 2 одиниці: чекати 2/2 с
    s.get_token_accounts_by_owner("O", deadline=d)          # 2 запити по 1: двічі по 1/2 с
    assert h.sleeps == pytest.approx([0.5, 1.0, 0.5, 0.5])
    assert [m for m, _ in seen] == ["getAccountInfo", "getSignaturesForAddress", "getAccountInfo",
                                    "getTransaction", "getTokenAccountsByOwner", "getTokenAccountsByOwner"]
    # одиниці списуються ДО відправки запиту, для кожного методу
    assert [t for _, t in seen] == pytest.approx([1.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert h.clock.monotonic() == pytest.approx(2.5)  # (1 + 1 + 1 + 2 + 2 − 2) / 2


def test_get_token_accounts_by_owner_costs_two_units():
    seen = []
    inner = all_methods_handler()

    def handler(request, h):
        seen.append(h.source._tokens)
        return inner(request, h)

    h = Harness(handler, rate_per_second=1.0, burst=2, max_batch=1)
    h.source.get_token_accounts_by_owner("O", deadline=h.deadline)
    assert len(h.requests) == 2 and h.sleeps == [] and seen == pytest.approx([1.0, 0.0])
    h.source.get_token_accounts_by_owner("O", deadline=h.deadline)
    # кожен з двох запитів (Token, Token-2022) окремо проходить кошик: по 1 с, а не одна пауза на 2 одиниці
    assert h.sleeps == pytest.approx([1.0, 1.0]) and seen[2:] == pytest.approx([0.0, 0.0])
    h = Harness(all_methods_handler(), rate_per_second=1.0, burst=1, max_batch=1)
    h.source.get_token_accounts_by_owner("O", deadline=h.deadline)
    assert h.sleeps == pytest.approx([1.0])  # кошик на 1: другий запит виклику вже чекає


def test_429_on_any_method_zeroes_the_shared_bucket():
    answers = [httpx.Response(429, json=RATE_LIMITED_429)]

    def handler(request, h):
        if answers:
            return answers.pop(0)
        return all_methods_handler()(request, h)

    h = Harness(handler, rate_per_second=2.0, burst=4, max_batch=4)
    h.source.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=h.deadline)
    # 429 -> 0; backoff 0.5 с поповнив 1 -> повтор без паузи пейсера; далі batch на 4: чекати (4 − 0) / 2
    assert h.sleeps == pytest.approx([0.5])
    h.source.get_transactions(list("abcd"), deadline=h.deadline)
    assert h.sleeps == pytest.approx([0.5, 2.0])


def test_global_limiter_wait_not_fitting_the_deadline_is_budget_for_single_calls():
    h = Harness(all_methods_handler(), rate_per_second=1.0, burst=1, max_batch=1)
    h.source.get_account_info("A", deadline=h.deadline)
    tokens, at = h.source._tokens, h.source._refilled_at
    short = Deadline(h.clock, 1.0)  # потрібно рівно 1 с ≥ remaining
    for c in (lambda: h.source.get_account_info("A", deadline=short),
              lambda: h.source.get_signatures_for_address("A", before=None, until=None, limit=5, deadline=short),
              lambda: h.source.get_token_accounts_by_owner("A", deadline=short)):
        with pytest.raises(RpcBudgetTimeout) as ei:
            c()
        assert ei.value.__cause__ is None and ei.value.__context__ is None
    assert len(h.requests) == 1 and h.sleeps == []
    assert (h.source._tokens, h.source._refilled_at) == (tokens, at)
    h.source.get_account_info("A", deadline=Deadline(h.clock, 1.01))
    assert h.sleeps == pytest.approx([1.0]) and len(h.requests) == 2


def _mixed_rpc(n_wallets=12, per_wallet=25):
    rpc = {"getAccountInfo": {}, "getSignaturesForAddress": {}, "getTransaction": {}, "getTokenAccountsByOwner": {}}
    for w in range(n_wallets):
        addr = f"W{w}"
        sigs = [f"{addr}s{i}" for i in range(per_wallet)]
        rpc["getSignaturesForAddress"][addr] = [sig_entry(s, 1000 - i) for i, s in enumerate(sigs)]
        rpc["getTransaction"].update({s: txr(w * 1000 + i) for i, s in enumerate(sigs)})
        rpc["getAccountInfo"][addr] = {"lamports": w}
        rpc["getTokenAccountsByOwner"][addr] = [{"pubkey": f"{addr}T", "account": {"owner": TOKEN_PROGRAM}},
                                                {"pubkey": f"{addr}T22", "account": {"owner": TOKEN_2022_PROGRAM}}]
    return rpc


def helius_harness(rpc, **source_kw):
    holder = {}
    source_kw.setdefault("rpc_cfg", cfg(max_retries=0))
    h = Harness(lambda request, h: _emulator(rpc, "finalized", holder["bucket"], shared=True)(request), **source_kw)
    holder["bucket"] = ProviderBucket(h.clock, capacity=HELIUS_CAPACITY, rate=HELIUS_RATE)
    return h, holder["bucket"]


def _run_mixed(h, rpc):
    """Мішаний збір по гаманцях: signatures + account_info + token_accounts (2) + transactions (25)."""
    units = 0
    for addr, history in rpc["getSignaturesForAddress"].items():
        d = Deadline(h.clock, 40.0)
        sigs = h.source.get_signatures_for_address(addr, before=None, until=None, limit=1000, deadline=d)
        assert [e["signature"] for e in sigs] == [e["signature"] for e in history]
        assert h.source.get_account_info(addr, deadline=d) == rpc["getAccountInfo"][addr]
        assert [e["pubkey"] for e in h.source.get_token_accounts_by_owner(addr, deadline=d)] == [
            f"{addr}T", f"{addr}T22"]
        got = h.source.get_transactions([e["signature"] for e in sigs], deadline=d)
        assert got == [rpc["getTransaction"][e["signature"]] for e in sigs]
        units += 1 + 1 + 2 + len(sigs)
    return units


def test_mixed_workload_against_shared_provider_bucket_never_hits_429():
    rpc = _mixed_rpc()
    h, bucket = helius_harness(rpc, profile="helius_free")
    units = _run_mixed(h, rpc)
    assert bucket.rejected == []
    batches = [len(b) for b in h.bodies() if isinstance(b, list)]
    assert max(batches) == 10  # max_batch профілю helius_free
    # рівно швидкість профілю (4.5/с) після початкового кошика 20: не швидше й без зайвих пауз (348 од. ≈ 72.9 с)
    assert h.clock.monotonic() == pytest.approx((units - 20) / 4.5)
    # без глобального лімітера той самий емулятор відповідає 429 — він не поблажливий, тест вище не вхолосту
    h, bucket = helius_harness(rpc, profile="helius_free", rate_per_second=math.inf)
    with pytest.raises(RpcRateLimited):
        _run_mixed(h, rpc)
    assert bucket.rejected


def test_helius_emulator_rejects_the_previous_helius_profile_and_rpcfast_start():
    # Закріплює знахідку живої перевірки: попередній профіль helius_free (8/с, кошик 10) і rpcfast_start (12/с,
    # кошик 30) на моделі «ємність 30, 5/с» ловлять 429 — емулятор не поблажливий; 4.5/20 — ні (тест вище).
    rpc = _mixed_rpc()
    for kw in ({"profile": "helius_free", "rate_per_second": 8.0, "burst": 10},
               {"profile": "rpcfast_start", "max_batch": 10},
               {"profile": "rpcfast_start"},
               {"profile": "helius_free", "rate_per_second": 5.5}):  # навіть трохи вище виміряних 5/с
        h, bucket = helius_harness(rpc, **kw)
        with pytest.raises(RpcRateLimited):
            _run_mixed(h, rpc)
        assert bucket.rejected, kw
    # межа моделі: рівно 5/с з кошиком 20 ще проходить (поповнення провайдера не повільніше за наше)
    h, bucket = helius_harness(rpc, profile="helius_free", rate_per_second=5.0)
    _run_mixed(h, rpc)
    assert bucket.rejected == []


def test_shared_emulator_rejects_batches_above_its_capacity_and_single_calls_too():
    # самоперевірка: одиночні запити справді списують з того ж кошика
    rpc = _mixed_rpc(n_wallets=1)
    h, bucket = helius_harness(rpc, profile="helius_free", rate_per_second=math.inf)
    for _ in range(HELIUS_CAPACITY):
        h.source.get_account_info("W0", deadline=h.deadline)
    with pytest.raises(RpcRateLimited):
        h.source.get_account_info("W0", deadline=h.deadline)
    rpc["getTransaction"].update({f"x{i}": None for i in range(35)})
    h, bucket = helius_harness(rpc, max_batch=30, burst=30, rate_per_second=math.inf)
    assert h.source.get_transactions([f"x{i}" for i in range(30)], deadline=h.deadline) == [None] * 30  # 30 так
    h, bucket = helius_harness(rpc, max_batch=35, burst=35, rate_per_second=math.inf)
    with pytest.raises(RpcRateLimited):  # 35 — ні, як на живому вузлі
        h.source.get_transactions([f"x{i}" for i in range(35)], deadline=h.deadline)


EXPECTED_PROFILES = {
    "rpcfast_start": {"rate_per_second": 12.0, "burst": 30, "max_batch": 25, "max_tx_version": 1},
    "helius_free": {"rate_per_second": 4.5, "burst": 20, "max_batch": 10, "max_tx_version": 1},
}


def _observe(n=60, **source_kw):
    """Розміри під-batch, паузи і версія транзакцій для n підписів — спостережувано, через запити."""
    sigs, results = sigs_and_results(n)
    h = Harness(batch_handler(results), budget=1000.0, **source_kw)
    assert h.source.get_transactions(sigs, deadline=h.deadline) == [txr(i) for i in range(n)]
    versions = {i["params"][1]["maxSupportedTransactionVersion"] for i in tx_items(h)}
    return [len(b) for b in h.bodies()], h.sleeps, versions


def test_profile_defaults_and_overrides():
    # таблиця профілів — golden (виміряні числа; не секрети, на результат не впливають) і незмінна
    assert {k: dict(v) for k, v in http_module.RPC_PROFILES.items()} == EXPECTED_PROFILES
    with pytest.raises(TypeError):
        http_module.RPC_PROFILES["x"] = {}
    with pytest.raises(TypeError):
        http_module.RPC_PROFILES["helius_free"]["burst"] = 99
    assert http_module.DEFAULT_PROFILE == "rpcfast_start"

    # за замовчуванням — rpcfast_start: під-batch 25, кошик 30, 12/с
    default = _observe()
    assert default == ([25, 25, 10], pytest.approx([20 / 12, 10 / 12]), {1})  # 30−25=5; (25−5)/12; 10/12
    assert _observe(profile="rpcfast_start") == default
    # helius_free: під-batch 10, кошик 20, 4.5/с: два під-batch без паузи, далі по 10/4.5 с
    assert _observe(profile="helius_free") == ([10] * 6, pytest.approx([10 / 4.5] * 4), {1})

    # явні параметри перекривають профіль — кожен окремо
    assert _observe(profile="helius_free", rate_per_second=4.0)[1] == pytest.approx([10 / 4] * 4)
    assert _observe(profile="helius_free", burst=30)[:2] == ([10] * 6, pytest.approx([10 / 4.5] * 3))
    assert _observe(profile="helius_free", max_batch=5)[0] == [5] * 12
    assert _observe(profile="helius_free", max_tx_version=0)[2] == {0}
    assert _observe(profile="rpcfast_start", max_batch=30, burst=30)[0] == [30, 30]
    assert _observe(profile="helius_free", rate_per_second=math.inf)[1] == []
    # перевірки застосовуються до підсумкових значень: max_batch з явного параметра проти burst профілю
    with pytest.raises(ValueError) as ei:
        HttpRpcSource(URL, CFG, commitment="finalized", profile="helius_free", max_batch=21)
    assert str(ei.value).startswith("max_batch:") and "burst" in str(ei.value)
    with pytest.raises(ValueError):
        HttpRpcSource(URL, CFG, commitment="finalized", burst=24)  # rpcfast_start: max_batch 25 > 24
    # from_env приймає профіль і ті самі перекриття
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(len(body))
        return httpx.Response(200, json=[{"jsonrpc": "2.0", "id": i["id"], "result": None} for i in body])

    clock = FakeClock()
    src = HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": URL},
                                 transport=httpx.MockTransport(handler), clock=clock, sleep=clock.advance,
                                 profile="helius_free")
    src.get_transactions([f"s{i}" for i in range(25)], deadline=Deadline(clock, 100))
    assert bodies == [10, 10, 5] and clock.monotonic() == pytest.approx(5 / 4.5)  # кошик 20: лише третій чекає
    bodies.clear()
    src = HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": URL},
                                 transport=httpx.MockTransport(handler), profile="helius_free", max_batch=3,
                                 rate_per_second=math.inf)
    src.get_transactions([f"s{i}" for i in range(7)], deadline=Deadline(FakeClock(), 100))
    assert bodies == [3, 3, 1]


BAD_PROFILES = ["helius", "", "RPCFAST_START", " rpcfast_start", None, 5, True, ["helius_free"], URL,
                f"helius_free{SECRET}"]


@pytest.mark.parametrize("profile", BAD_PROFILES, ids=repr)
def test_unknown_profile_and_old_parameter_names_are_rejected(profile):
    requests = []
    transport = httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200))
    for make in (
        lambda: HttpRpcSource(URL, CFG, transport=transport, commitment="finalized", profile=profile),
        lambda: HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": URL},
                                       transport=transport, profile=profile),
    ):
        with pytest.raises(ValueError) as ei:
            make()
        exc = ei.value
        assert type(exc) is ValueError and str(exc).startswith("profile:")
        rendered = "".join(traceback.format_exception(exc))
        assert SECRET not in rendered and "rpc.example" not in rendered
        if isinstance(profile, str) and profile:
            assert repr(profile) not in rendered and f"{profile!s}" not in str(exc).replace(
                "rpcfast_start", "").replace("helius_free", "")
        assert exc.__cause__ is None and exc.__context__ is None
    assert requests == []
    # старі імена (T-049) — не тихі аліаси: TypeError і в конструкторі, і в from_env
    for old in ({"tx_rate_per_second": 12.0}, {"tx_burst": 30}):
        with pytest.raises(TypeError):
            HttpRpcSource(URL, CFG, commitment="finalized", **old)
        with pytest.raises(TypeError):
            HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": URL}, **old)


URL_ALT = "https://alt.example/?api-key=ALTSECRET456"


def test_from_env_url_var_selects_variable_without_leaking_value():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return good(request)

    environ = {"UNMASK_RPC_URL": URL, "UNMASK_RPC_URL_ALT": URL_ALT}
    for var, want in ((None, URL), ("UNMASK_RPC_URL", URL), ("UNMASK_RPC_URL_ALT", URL_ALT)):
        kw = {} if var is None else {"url_var": var}
        src = HttpRpcSource.from_env(CFG, commitment="finalized", environ=environ,
                                     transport=httpx.MockTransport(handler), **kw)
        src.get_account_info("A", deadline=Deadline(FakeClock(), 10))
        assert seen[-1] == want
        assert "ALTSECRET456" not in repr(src) and SECRET not in repr(src)
    # змінна відсутня / порожня / з невалідним URL: ConfigError з ІМЕНЕМ змінної, без жодного значення
    for env in ({"UNMASK_RPC_URL": URL}, {"UNMASK_RPC_URL": URL, "UNMASK_RPC_URL_ALT": ""},
                {"UNMASK_RPC_URL": URL, "UNMASK_RPC_URL_ALT": "ftp://alt.example/?k=ALTSECRET456"},
                {"UNMASK_RPC_URL": URL, "UNMASK_RPC_URL_ALT": "https://alt.example:ALTSECRET456/"}):
        with pytest.raises(ConfigError) as ei:
            HttpRpcSource.from_env(CFG, commitment="finalized", environ=env, url_var="UNMASK_RPC_URL_ALT")
        exc = ei.value
        assert "UNMASK_RPC_URL_ALT" in str(exc)
        rendered = "".join(traceback.format_exception(exc))
        assert "ALTSECRET456" not in rendered and SECRET not in rendered and "example" not in rendered
        assert exc.__cause__ is None and exc.__context__ is None
    # ім'я змінної — лише ім'я: інакше (напр. помилково переданий сам URL) — ValueError без відлуння
    for bad in (URL, "", "UNMASK RPC", "1ABC", None, 5, b"UNMASK_RPC_URL", "unmask-rpc"):
        with pytest.raises(ValueError) as ei:
            HttpRpcSource.from_env(CFG, commitment="finalized", environ={**environ, str(bad): URL}, url_var=bad)
        rendered = "".join(traceback.format_exception(ei.value))
        assert str(ei.value).startswith("url_var:")
        assert SECRET not in rendered and "rpc.example" not in rendered
        assert ei.value.__cause__ is None and ei.value.__context__ is None


@pytest.mark.parametrize("scenario", ["basic", "hub"])
def test_helius_profile_core_collect_over_emulator_completes_without_rate_limit(scenario):
    # Ядро шле пачки по rpc.tx_batch_size=25; профіль helius_free ділить їх на під-batch по 10 (all-or-nothing
    # лишається на рівні виклику). Емулятор — модель Helius free: один кошик ємністю 30, 5/с, на ВСІ методи.
    directory = SCENARIOS / scenario
    rpc = json.loads((directory / "rpc.json").read_text())
    expected = json.loads((directory / "expected.json").read_text())
    config = load_config(Path(__file__).parent.parent / "config" / "ingest.yaml")
    if "config" in expected:
        config = dataclasses.replace(config, **expected["config"])
    assert config.rpc.tx_batch_size == 25
    mint = expected["mint"]
    from_fixture = IngestService(config, FixtureRpcSource(directory), clock=FakeClock()).collect(mint)

    def run(rpc_config=config.rpc, **source_kw):
        source_clock = FakeClock()
        bucket = ProviderBucket(source_clock, capacity=HELIUS_CAPACITY, rate=HELIUS_RATE)
        emulator = _emulator(rpc, config.commitment, bucket, shared=True)
        sizes: list[int] = []

        def recording(request):
            body = json.loads(request.content)
            sizes.append(len(body) if isinstance(body, list) else 1)
            return emulator(request)

        src = HttpRpcSource(URL, rpc_config, transport=httpx.MockTransport(recording),
                            commitment=config.commitment, clock=source_clock, sleep=source_clock.advance,
                            **source_kw)
        # дедлайн сервісу — окремий FakeClock: тест про ліміт швидкості, а не про бюджет часу
        run_config = dataclasses.replace(config, rpc=rpc_config)
        return IngestService(run_config, src, clock=FakeClock()).collect(mint), bucket, sizes, source_clock

    result, bucket, sizes, source_clock = run(profile="helius_free")
    out = to_dict(result)
    assert _stable(result) == _stable(from_fixture)
    assert out["completeness"]["status"] == "complete"
    assert not [m for m in out["completeness"]["missing"] if m["reason"] == "rate_limited"]
    assert bucket.rejected == []
    assert max(sizes) <= 10 and len(bucket.accepted) == len(sizes)
    assert source_clock.monotonic() >= (sum(sizes) - 20) / 4.5 - 1e-9  # не швидше за 4.5/с: (Σ − 20) / 4.5
    if scenario == "hub":
        assert sizes.count(10) > 2  # пачки ядра по 25 справді розбито на під-batch по 10

    # без повторів (rpc.max_retries=0) лімітер сам по собі дає повний результат — не повтори його рятують
    no_retries = dataclasses.replace(config.rpc, max_retries=0)
    result, bucket, _, _ = run(no_retries, profile="helius_free")
    assert _stable(result) == _stable(from_fixture)
    assert to_dict(result)["completeness"]["status"] == "complete" and bucket.rejected == []

    # без глобального лімітера той самий збір ловить ліміт провайдера — тест вище не вхолосту. З повторами
    # ядра (max_retries=2, backoff 0.5·2^n) емулятор встигає поповнитись, тому — без повторів, як і вище.
    result, bucket, _, _ = run(no_retries, profile="helius_free", rate_per_second=math.inf)
    out = to_dict(result)
    assert bucket.rejected
    assert out["completeness"]["status"] == "incomplete"
    assert [m for m in out["completeness"]["missing"] if m["reason"] == "rate_limited"]


# --------------------------------------------------------------------------- T-053: змагальні тести секретів


@pytest.mark.parametrize("kw, prefix", [
    ({"profile": URL}, "profile:"),
    ({"profile": f"x{SECRET}"}, "profile:"),
    ({"rate_per_second": URL}, "rate_per_second:"),
    ({"rate_per_second": float("nan")}, "rate_per_second:"),
    ({"burst": URL, "max_batch": 1}, "burst:"),
    ({"burst": 3}, "max_batch:"),
    ({"max_batch": URL}, "max_batch:"),
])
def test_t053_parameter_errors_carry_no_value_and_no_url(kw, prefix, caplog):
    with caplog.at_level(1):
        with pytest.raises(ValueError) as ei:
            HttpRpcSource(URL, CFG, commitment="finalized", **kw)
    exc = ei.value
    assert type(exc) is ValueError and str(exc).startswith(prefix)
    assert_clean(exc, [SECRET, URL, "rpc.example"])
    assert exc.__cause__ is None and exc.__context__ is None
    assert SECRET not in caplog.text and "rpc.example" not in caplog.text


def test_t053_errors_raised_through_from_env_carry_no_url_either():
    for kw in ({"profile": URL}, {"rate_per_second": 0.0}, {"burst": 0}, {"url_var": URL}):
        with pytest.raises(ValueError) as ei:
            HttpRpcSource.from_env(CFG, commitment="finalized", environ={"UNMASK_RPC_URL": URL}, **kw)
        assert_clean(ei.value, [SECRET, "rpc.example"])
        assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_t053_url_var_pointing_to_a_variable_with_a_leaky_url_is_config_error_without_it():
    for leaky in LEAKY_URLS:
        with pytest.raises(ConfigError) as ei:
            HttpRpcSource.from_env(CFG, commitment="finalized", environ={"ALT_RPC": leaky}, url_var="ALT_RPC")
        assert "SECRETKEY99" not in "".join(traceback.format_exception(ei.value))
        assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_t053_repr_and_failures_under_profiles_do_not_leak_the_url(caplog):
    for profile in http_module.RPC_PROFILES:
        h = Harness(one_shot(httpx.Response(429, text=f"{URL} {SECRET}")), profile=profile,
                    rpc_cfg=cfg(max_retries=1, retry_backoff_seconds=0.0))
        with caplog.at_level(1):
            with pytest.raises(RpcError) as ei:
                h.source.get_token_accounts_by_owner("O", deadline=h.deadline)
        assert_clean(ei.value, [SECRET])
        assert ei.value.__cause__ is None and ei.value.__context__ is None
        assert SECRET not in repr(h.source) + str(h.source) + caplog.text
        assert "rpc.example" not in repr(h.source) and len(h.requests) == 2  # Token: 1 + 1 повтор; Token-2022 не відправлено


def _raises_in(func: ast.FunctionDef):
    for node in ast.walk(func):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            yield node.exc


def test_t053_validation_messages_are_not_built_from_external_values():
    # Статично: у конструкторі й `from_env` тексти помилок — константи; f-рядок/конкатенація/format
    # дозволені лише з іменами модульних констант і `url_var` (ім'я змінної, провалідоване регексом до
    # використання). Виняток — дотеперішнє `commitment` (не секрет, порівнюється з фіксованим списком).
    tree = ast.parse(Path(http_module.__file__).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HttpRpcSource")
    funcs = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ("__init__", "from_env")]
    assert len(funcs) == 2
    allowed = {"url_var", "ENV_URL", "_INVALID_URL", "_PROFILE_ERROR"}
    checked = 0
    for func in funcs:
        for call_node in _raises_in(func):
            for arg in call_node.args:
                names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
                if "commitment" in names and func.name == "__init__":
                    continue
                assert not any(isinstance(n, ast.Call) for n in ast.walk(arg)), ast.unparse(arg)
                assert names <= allowed, ast.unparse(arg)
                checked += 1
    assert checked >= 8
