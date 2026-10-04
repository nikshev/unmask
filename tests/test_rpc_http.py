# verifies: FR-001-15
"""HttpRpcSource: JSON-RPC 2.0 через httpx.MockTransport — жодного сокета (принцип II).

Усе, що тут моделюється «мережею», — це `httpx.MockTransport(handler)`: handler бачить справжній
`httpx.Request` (тіло, заголовки, таймаути) і повертає `httpx.Response` або кидає httpx-виняток.
Час — `FakeClock`; пауза між повторами — `sleep`, що просуває той самий годинник.
"""

import copy
import dataclasses
import json
import logging
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

    def __init__(self, handler, *, rpc_cfg=CFG, commitment="finalized", budget=40.0, url=URL):
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
        "params": ["s1", {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                          "commitment": "finalized"}],
    }]
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


def test_batch_is_chunked_by_page_size_and_order_is_kept_across_chunks():
    sigs = [f"s{i}" for i in range(5)]
    results = {s: (None if i == 3 else txr(i)) for i, s in enumerate(sigs)}
    h = Harness(batch_handler(results, shuffle=True), rpc_cfg=cfg(page_size=2))
    got = h.source.get_transactions(sigs, deadline=h.deadline)
    assert got == [txr(0), txr(1), txr(2), None, txr(4)]
    assert [len(b) for b in h.bodies()] == [2, 2, 1]  # кожен запит ≤ rpc.page_size


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

    h = Harness(burn, budget=50.0, rpc_cfg=cfg(page_size=1, request_timeout_seconds=1000))
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


def _emulator(rpc: dict, commitment: str):
    """JSON-RPC сервер поверх записаного rpc.json; batch відповідає у ЗВОРОТНОМУ порядку."""

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
            assert params[1] == {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                 "commitment": commitment}
            return copy.deepcopy(rpc["getTransaction"].get(params[0]))
        if method == "getTokenAccountsByOwner":
            program = params[1]["programId"]
            listed = rpc["getTokenAccountsByOwner"].get(params[0], [])
            chosen = [e for e in listed if (e.get("account", {}).get("owner") or TOKEN_PROGRAM) == program]
            return {"context": {"slot": 1}, "value": copy.deepcopy(chosen)}
        raise AssertionError(method)

    def handler(request):
        body = json.loads(request.content)
        if isinstance(body, list):
            out = [{"jsonrpc": "2.0", "id": x["id"], "result": answer(x)} for x in body]
            return httpx.Response(200, json=out[::-1])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": answer(body)})

    return handler


def _stable(result) -> dict:
    d = to_dict(result)
    for key in _VOLATILE:
        d.get("metadata", {}).pop(key, None)
    return d


@pytest.mark.parametrize("scenario", ["basic", "hub"])
def test_ingest_service_over_http_source_equals_fixture_source(scenario):
    directory = SCENARIOS / scenario
    rpc = json.loads((directory / "rpc.json").read_text())
    expected = json.loads((directory / "expected.json").read_text())
    config = load_config(Path(__file__).parent.parent / "config" / "ingest.yaml")
    if "config" in expected:
        config = dataclasses.replace(config, **expected["config"])
    mint = expected["mint"]

    from_fixture = IngestService(config, FixtureRpcSource(directory), clock=FakeClock()).collect(mint)
    http_source = HttpRpcSource(
        URL, config.rpc, transport=httpx.MockTransport(_emulator(rpc, config.commitment)),
        commitment=config.commitment, clock=FakeClock(), sleep=lambda s: None,
    )
    from_http = IngestService(config, http_source, clock=FakeClock()).collect(mint)

    assert to_dict(from_http)["metadata"]["source"] == "http"
    assert _stable(from_http) == _stable(from_fixture)
    assert _stable(from_http)["completeness"]["status"] == "complete"


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
