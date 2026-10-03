# impl: FR-001-15
"""Фікстурне джерело: повтор записаних відповідей Solana JSON-RPC без мережі.

`rpc.json` сценарію: `{"getAccountInfo": {addr: value|null},
"getSignaturesForAddress": {addr: [SignatureInfo...]}, "getTransaction":
{sig: RawTransaction|null}, "getTokenAccountsByOwner": {owner: [TokenAccountInfo...]}}`.
Історія підписів зберігається повністю, від найновішого; `before`/`until`/`limit`
емулюються поверх неї з семантикою живого RPC.

Збої й час задаються в конструкторі (не у файлі сценарію):
- `FailAfter(n, exc)` — n-й виклик (рахунок з 1, усі методи разом) і кожен наступний
  піднімає `exc` (ліміт, що не відпускає);
- `FailFor(key, exc, times)` — перші `times` викликів, у параметрах яких є `key`
  (адреса або підпис), піднімають `exc`; далі джерело відпускає;
- `clock` — `FakeClock`, який просувається на `advance_per_call` на кожному виклику.

Журнал `calls` — по одному запису `(method, params)` на виклик публічного методу
(також невдалий); `get_transactions` пише весь пакет підписів одним записом.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from unmask.ingest.budget import FakeClock
from unmask.ingest.rpc.protocol import (
    AccountInfo,
    Deadline,
    RawTransaction,
    SignatureInfo,
    TokenAccountInfo,
)


@dataclass
class FailAfter:
    n: int
    exc: Exception

    def __post_init__(self) -> None:
        if self.n < 1:
            raise ValueError("FailAfter.n must be >= 1")


@dataclass
class FailFor:
    key: str
    exc: Exception
    times: int
    _raised: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.times < 1:
            raise ValueError("FailFor.times must be >= 1")


FailurePolicy = FailAfter | FailFor


def _keys_of(params: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for value in params.values():
        if isinstance(value, str):
            keys.add(value)
        elif isinstance(value, (list, tuple)):
            keys.update(v for v in value if isinstance(v, str))
    return keys


class FixtureRpcSource:
    def __init__(
        self,
        scenario_dir: str | Path,
        *,
        failures: Sequence[FailurePolicy] | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        self._dir = Path(scenario_dir)
        with (self._dir / "rpc.json").open(encoding="utf-8") as fh:
            data = json.load(fh)
        self._accounts: dict[str, Any] = data.get("getAccountInfo", {})
        self._signatures: dict[str, list[SignatureInfo]] = data.get("getSignaturesForAddress", {})
        self._transactions: dict[str, Any] = data.get("getTransaction", {})
        self._token_accounts: dict[str, list[TokenAccountInfo]] = data.get(
            "getTokenAccountsByOwner", {}
        )
        self._failures = list(failures or [])
        self._clock = clock
        self._call_count = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return f"fixture:{self._dir.name}"

    # --- службове ---------------------------------------------------------------

    def _enter(self, method: str, params: dict[str, Any]) -> None:
        self._call_count += 1
        self.calls.append((method, copy.deepcopy(params)))
        if self._clock is not None:
            self._clock.tick()
        keys = _keys_of(params)
        for policy in self._failures:
            if isinstance(policy, FailAfter):
                if self._call_count >= policy.n:
                    raise policy.exc
            elif policy.key in keys and policy._raised < policy.times:
                policy._raised += 1
                raise policy.exc

    # --- RpcSource --------------------------------------------------------------

    def get_account_info(self, address: str, *, deadline: Deadline) -> AccountInfo | None:
        self._enter("getAccountInfo", {"address": address})
        return copy.deepcopy(self._accounts.get(address))

    def get_signatures_for_address(
        self,
        address: str,
        *,
        before: str | None,
        until: str | None,
        limit: int,
        deadline: Deadline,
    ) -> list[SignatureInfo]:
        self._enter(
            "getSignaturesForAddress",
            {"address": address, "before": before, "until": until, "limit": limit},
        )
        history = self._signatures.get(address)
        if not history:
            return []  # адреси немає у файлі: порожня історія, не помилка
        order = [entry["signature"] for entry in history]

        def position(cursor: str) -> int:
            try:
                return order.index(cursor)
            except ValueError:
                raise ValueError(
                    f"cursor {cursor!r} is not in the recorded history of {address!r}"
                ) from None

        start = position(before) + 1 if before is not None else 0
        stop = position(until) if until is not None else len(history)
        return copy.deepcopy(history[start:stop][:limit])

    def get_transactions(
        self, signatures: Sequence[str], *, deadline: Deadline
    ) -> list[RawTransaction | None]:
        self._enter("getTransaction", {"signatures": list(signatures)})
        return [copy.deepcopy(self._transactions.get(sig)) for sig in signatures]

    def get_token_accounts_by_owner(
        self, owner: str, *, deadline: Deadline
    ) -> list[TokenAccountInfo]:
        self._enter("getTokenAccountsByOwner", {"owner": owner})
        return copy.deepcopy(self._token_accounts.get(owner, []))
