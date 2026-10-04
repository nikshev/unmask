# trace: ignore-file
"""Генератор фікстур графа фінансування (фіча 002, T-026; розширено T-056: пил, `dust_fanout`, версія 2).

Запуск: `uv run python tests/fixtures/build_graph_fixtures.py [--check]` (без аргументів перезаписує
`tests/fixtures/graph/<scenario>/{ingest.json, expected.json}`; `--check` лише порівнює з диском і
повертає 1, якщо є розбіжні, відсутні чи зайві файли; нічого не пише).

Принципи (research R-18, уроки фічі 001: `tests/fixtures/build_fixtures.py`)
---------------------------------------------------------------------------
* Не імпортує `unmask`: генератор — еталон, незалежний від коду, що перевіряється (`unmask.graph`,
  `unmask.hubs`). Лише stdlib і `solders` (справжні base58-адреси, ключі на кривій ed25519 і PDA
  поза кривою). Перевіряється тестом за `ast`.
* Детермінований: адреси й підписи виводяться з міток через sha256/sha512, жодного `random`/`time`;
  дві збірки поспіль збігаються побайтно.
* Сценарій описується ДЕКЛАРАТИВНО: гаманці (мітка -> звичайний / PDA / фіксована адреса), покупці,
  перекази (tx, шлях інструкції, відправник, отримувач, слот, актив, сума, глибина), нерозгорнуті
  вершини, `missing`, а також ОГОЛОШЕНІ вершини (ролі й глибина) та ОГОЛОШЕНІ хаби/позначки покупців
  із причинами. `ingest.json` — результат збору за схемою 001 (форма `to_dict`), складений із цього опису.
* `expected.json` — оракул простими словниками й перебором, а НЕ прогоном коду, що тестується:
    - ребра — агрегація переказів словником за `(відправник, отримувач, актив)`;
    - виміри — перебір усіх вершин: унікальні контрагенти, відправники, лічильники переказів;
    - критерії хаба — буквальне застосування задекларованих порогів (`>` для `degree`/`one_off_senders`,
      `<` для `dust_fanout`; передумови `>=`) із `expected.config`; запис відсікання несе виміряне значення,
      поріг, версії та інцидентні ребра;
    - компоненти — BFS (у коді й тесті використовується union-find: інша реалізація);
    - попередження — за правилами research R-10/R-16.
  Після побудови генератор звіряє оракул з ОГОЛОШЕНИМИ ролями, глибинами й хабами сценарію: розбіжність —
  помилка генератора (`AssertionError`), а не мовчазно записаний еталон.

Правила еталона (research R-5…R-10, R-15, R-16)
-----------------------------------------------
* Ребро `transfer` = `(sender, receiver, asset)`: `amount` — сума, `count` — кількість переказів
  (`== len(refs)`), `first/last_slot|time` — за впорядкованими посиланнями `(slot, signature, path)`.
* Глибина вершини = мінімум за правилами: покупець 0; отримувач переказу <= `depth - 1`;
  відправник <= `depth`. Ролі: `buyer` (є в `buyers`), `funder` (відправник хоча б одного переказу).
* `degree` — унікальні контрагенти (вхідні ∪ вихідні, усі активи); `unique_senders` — відправники
  вхідних переказів; `one_off_senders` — з них із сумарним `count == 1`; частка = їх відношення.
* Пил (research R-22, FR-002-22): `buyer_fanout` — кількість різних покупців, до яких є ребро
  `(transfer, v, b, sol)` (SPL і не-покупці не рахуються); `median_to_buyers` — ВЕРХНЯ медіана сум цих ребер
  (одне значення на покупця): `sorted(amounts)[len(amounts) // 2]`, лампорти; `None` при `buyer_fanout == 0`.
* Критерії (хит = окремий запис): `known_list` (`list:<категорія>` | `address_type:off_curve`),
  `degree` (`> degree_threshold`), `dust_fanout` (`buyer_fanout >= dust_min_fanout` і
  `median_to_buyers < dust_amount_lamports`, строго; `detail = measured`), `one_off_senders`
  (`unique_senders >= one_off_min_senders` і частка `> one_off_senders_share`), `ingest_high_degree`
  (нерозгорнута `high_degree`, `measured = counterparties_seen`, `threshold = metadata.counterparty_threshold`).
  `signature_cap` критерієм не є. Хити впорядковано за `(criterion, detail)` — рядковим значенням `criterion`:
  `degree < dust_fanout < ingest_high_degree < known_list < one_off_senders`.
* Не-покупець із >= 1 хітом -> запис відсікання; покупець із >= 1 хітом -> позначка (лишається).
* Звіт: знімки до/після (слабка зв'язність; частка = покупці найбільшої за покупцями компоненти /
  усі покупці), попередження `giant_component` (частка після `>` порогу), `empty_graph` (0 покупців),
  `all_sources_pruned` (були не-покупці й жодного не лишилось), `address_lists_not_applied`.
  `delegated_incomplete` тут НЕ входить: поле `delegated` у результаті 001 з'явиться з T-044, і
  попередження залежить від нього, а не від графа чи відсікання; сценарій `g_delegated` — T-047.

Когерентність із збором 001 (research R-3 фічі 001): вершина `high_degree` має рівно
`counterparty_threshold` зібраних відправників і `counterparties_seen = поріг + 1`; жодна вершина не має
більше зібраних відправників, ніж поріг. Тому у сценаріях із малою нерозгорнутою вершиною
`metadata.counterparty_threshold` оголошено малим числом (а не 200).
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import deque
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

OUT_DIR = Path(__file__).parent / "graph"

SCENARIO_NAMES = (
    "g_basic", "g_hub", "g_known", "g_buyer_hub", "g_incomplete", "g_empty", "g_all_hubs", "g_unexpanded",
    "g_dust", "g_financier", "g_dust_mixed",
)

CONFIG_VERSION = 2  # версія hubs.yaml, за якою складено еталон (v2 = + dust_*; research R-22)

SOL = 10**9
BASE_TIME = 1_759_400_000
ANALYZED_AT = 1_759_500_000
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # Pump.fun, on-curve програма (категорія launchpads)

DEFAULT_THRESHOLDS = {
    "degree_threshold": 100,
    "one_off_senders_share": 0.8,
    "one_off_min_senders": 10,
    "giant_component_warn_share": 0.5,
    "prune_off_curve": True,
    "prune_ingest_high_degree": True,
    "dust_amount_lamports": 1_000_000,
    "dust_min_fanout": 5,
}

# Списки адрес сценарію: стан списків v1 (research R-13). `exchanges`/`market_makers` порожні.
DEFAULT_LISTS = {
    "version": 1,
    "categories": {
        "system_programs": [
            "11111111111111111111111111111111",
            "ComputeBudget111111111111111111111111111111",
            "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        ],
        "token_programs": [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
            "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        ],
        "dex_routers": ["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"],
        "amm_programs": [
            "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
            "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
        ],
        "launchpads": [PUMP],
        "exchanges": [],
        "market_makers": [],
    },
}


# ---------------------------------------------------------------------------------------
# Адреси й підписи
# ---------------------------------------------------------------------------------------


def _digest(*parts: str, algo: str = "sha256") -> bytes:
    return hashlib.new(algo, "/".join(("unmask-graph-fixture",) + parts).encode()).digest()


def _wallet_address(scenario: str, label: str) -> str:
    return str(Keypair.from_seed(_digest(scenario, "wallet", label)).pubkey())


def _pda_address(scenario: str, label: str) -> str:
    program = Pubkey.from_string(_wallet_address(scenario, "PDA_PROGRAM"))
    address, _bump = Pubkey.find_program_address([b"unmask-fixture", _digest(scenario, "pda", label)[:16]], program)
    assert not address.is_on_curve()
    return str(address)


def _signature(scenario: str, label: str) -> str:
    return str(Signature.from_bytes(_digest(scenario, "tx", label, algo="sha512")))


def _path_key(path: str) -> tuple[int, ...]:
    return tuple(int(part) for part in path.split("."))


# ---------------------------------------------------------------------------------------
# Декларативний опис сценарію
# ---------------------------------------------------------------------------------------


class Scenario:
    """Декларація сценарію: нічого не обчислює, лише збирає факти, задані автором."""

    def __init__(self, name: str, description: str, *, first_buyers_n: int, counterparty_threshold: int = 200,
                 thresholds: dict | None = None, lists: dict | None = DEFAULT_LISTS) -> None:
        self.name = name
        self.description = description
        self.first_buyers_n = first_buyers_n
        self.counterparty_threshold = counterparty_threshold
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.lists = lists
        self.wallets: dict[str, str] = {}
        self.kinds: dict[str, str] = {}
        self.buyers: list[dict] = []
        self.transfers: list[dict] = []
        self.unexpanded: list[dict] = []
        self.missing: list[dict] = []
        self.buyers_complete: tuple[bool, str | None, str] = (True, None, "")
        self.vertices: dict[str, tuple[tuple[str, ...], int]] = {}
        self.hubs: dict[str, tuple[str, ...]] = {}
        self.flags: dict[str, tuple[str, ...]] = {}
        self.wallet("M")
        self.wallet("DEX")

    # --- гаманці ---
    def wallet(self, label: str) -> str:
        return self._add(label, "wallet", _wallet_address(self.name, label))

    def pda(self, label: str) -> str:
        return self._add(label, "pda", _pda_address(self.name, label))

    def fixed(self, label: str, address: str) -> str:
        return self._add(label, "wallet", address)

    def _add(self, label: str, kind: str, address: str) -> str:
        assert label not in self.wallets, f"{self.name}: дубль мітки {label}"
        self.wallets[label] = address
        self.kinds[label] = kind
        return address

    def a(self, label: str) -> str:
        return self.wallets[label]  # KeyError на невідомій мітці — опечатка в декларації

    # --- факти ---
    def buy(self, label: str, slot: int, *, spent: list[tuple[str, int]], received: int) -> None:
        self.buyers.append(dict(label=label, slot=slot, spent=spent, received=received))

    def transfer(self, tx: str, path: str, sender: str, receiver: str, slot: int, amount: int, depth: int, *,
                 asset: str = "sol", decimals: int | None = None) -> None:
        self.transfers.append(dict(tx=tx, path=path, sender=sender, receiver=receiver, slot=slot,
                                   amount=amount, depth=depth, asset=asset, decimals=decimals))

    def declare(self, labels, roles: tuple[str, ...], depth: int) -> None:
        for label in labels:
            assert label not in self.vertices, f"{self.name}: вершину {label} оголошено двічі"
            self.vertices[label] = (tuple(sorted(roles)), depth)

    def spl(self, label: str) -> str:
        return "spl:" + self.a(label)


# ---------------------------------------------------------------------------------------
# Ingest-документ за схемою 001 (форма to_dict)
# ---------------------------------------------------------------------------------------


def build_ingest(s: Scenario) -> dict:
    address_type = {label: ("off_curve" if kind == "pda" else "wallet") for label, kind in s.kinds.items()}
    buyer_rows = []
    for b in s.buyers:
        sig = _signature(s.name, "buy_" + b["label"])
        spent = [{"asset": ("sol" if asset == "sol" else s.spl(asset[4:])), "amount": amount}
                 for asset, amount in b["spent"]]
        buyer_rows.append({
            "wallet": s.a(b["label"]), "rank": 0, "first_buy_signature": sig, "first_buy_slot": b["slot"],
            "first_buy_time": BASE_TIME + b["slot"], "received_amount": b["received"], "spent": spent,
            "programs": [s.a("DEX")], "address_type": address_type[b["label"]],
        })
    buyer_rows.sort(key=lambda r: (r["first_buy_slot"], r["first_buy_signature"], r["wallet"]))
    for rank, row in enumerate(buyer_rows, start=1):
        row["rank"] = rank

    transfer_rows = []
    for t in s.transfers:
        asset = t["asset"] if t["asset"] == "sol" else s.spl(t["asset"][4:])
        transfer_rows.append({
            "signature": _signature(s.name, t["tx"]), "slot": t["slot"], "block_time": BASE_TIME + t["slot"],
            "instruction_path": t["path"], "sender": s.a(t["sender"]), "receiver": s.a(t["receiver"]),
            "asset": asset, "amount": t["amount"], "decimals": t["decimals"], "depth": t["depth"],
        })
    transfer_rows.sort(key=lambda r: (r["slot"], r["signature"], _path_key(r["instruction_path"])))

    unexpanded_rows = sorted(
        ({"wallet": s.a(u["wallet"]), "depth": u["depth"], "reason": u["reason"],
          "counterparties_seen": u["counterparties_seen"], "signatures_seen": u["signatures_seen"],
          "signatures_truncated": u["signatures_truncated"]} for u in s.unexpanded),
        key=lambda r: (r["depth"], r["wallet"]))
    missing_rows = sorted(
        ({"wallet": s.a(m["wallet"]), "depth": m["depth"], "reason": m["reason"], "detail": m["detail"]}
         for m in s.missing),
        key=lambda r: (r["depth"], r["wallet"], r["reason"]))
    complete, reason, detail = s.buyers_complete
    status = "complete" if not missing_rows and complete else "incomplete"
    assert len({(r["signature"], r["instruction_path"]) for r in transfer_rows}) == len(transfer_rows)
    assert all(r["sender"] != r["receiver"] for r in transfer_rows)

    return {
        "metadata": {
            "mint": s.a("M"), "analyzed_at": ANALYZED_AT, "wallets_analyzed": len(buyer_rows),
            "config_version": 2, "first_buyers_n": s.first_buyers_n, "funding_depth": 3,
            "counterparty_threshold": s.counterparty_threshold, "max_signatures_per_wallet": 300,
            "collect_spl_inbound": True, "time_budget_seconds": 40.0, "elapsed_seconds": 1.5,
            "rpc_calls": 10 + len(transfer_rows), "transactions_scanned": len({r["signature"] for r in transfer_rows}),
            "source": f"fixture:{s.name}", "resumed": False, "served_from_cache": False,
        },
        "completeness": {
            "status": status, "missing": missing_rows,
            "buyers": {"complete": complete, "reason": reason, "detail": detail},
        },
        "buyers": buyer_rows,
        "transfers": transfer_rows,
        "unexpanded": unexpanded_rows,
        # схема 1.1 (T-044): аналіз swap-and-send виконано над тим самим вікном, що й перелічення покупців,
        # тож його повнота дзеркалить buyers; делегованих зв'язків у цих сценаріях немає (g_delegated — T-047).
        "delegated": {"links": [], "unpaired": [], "complete": complete, "reason": reason, "detail": detail},
    }


# ---------------------------------------------------------------------------------------
# Оракул: прості словники, перебір, BFS. Жодного коду фічі 002.
# ---------------------------------------------------------------------------------------


def edge_key(e: dict) -> tuple:
    return (e["kind"], e["sender"], e["receiver"], e["asset"] or "")


def aggregate_edges(transfers: list[dict]) -> list[dict]:
    """Перекази -> ребра: словник за `(sender, receiver, asset)`."""
    groups: dict[tuple, list[dict]] = {}
    for t in transfers:
        groups.setdefault((t["sender"], t["receiver"], t["asset"]), []).append(t)
    edges = []
    for (sender, receiver, asset), items in groups.items():
        items = sorted(items, key=lambda t: (t["slot"], t["signature"], _path_key(t["instruction_path"])))
        decimals = {t["decimals"] for t in items}
        assert len(decimals) == 1, f"розбіжні decimals у {sender}->{receiver} {asset}"
        edges.append({
            "kind": "transfer", "sender": sender, "receiver": receiver, "asset": asset,
            "amount": sum(t["amount"] for t in items), "decimals": items[0]["decimals"],
            "count": len(items), "first_slot": items[0]["slot"], "last_slot": items[-1]["slot"],
            "first_time": items[0]["block_time"], "last_time": items[-1]["block_time"],
            "refs": [{"signature": t["signature"], "slot": t["slot"], "instruction_path": t["instruction_path"]}
                     for t in items],
        })
    edges.sort(key=edge_key)
    return edges


def oracle_measures(address: str, edges: list[dict], buyers: set[str]) -> dict:
    """Виміри вершини перебором усіх ребер (R-7, R-8, R-22); `buyers` — множина адрес покупців."""
    counterparties = set()
    for e in edges:
        if e["sender"] == address:
            counterparties.add(e["receiver"])
        if e["receiver"] == address:
            counterparties.add(e["sender"])
    senders: dict[str, int] = {}
    for e in edges:
        if e["kind"] == "transfer" and e["receiver"] == address:
            senders[e["sender"]] = senders.get(e["sender"], 0) + e["count"]
    one_off = sum(1 for total in senders.values() if total == 1)
    # пил: одна сума на покупця = сума ребра `(transfer, address, покупець, sol)`; SPL і не-покупці не рахуються
    to_buyers: dict[str, int] = {}
    for e in edges:
        if (e["kind"] == "transfer" and e["sender"] == address and e["asset"] == "sol"
                and e["receiver"] in buyers):
            to_buyers[e["receiver"]] = to_buyers.get(e["receiver"], 0) + e["amount"]
    amounts = sorted(to_buyers.values())
    return {
        "degree": len(counterparties), "unique_senders": len(senders), "one_off_senders": one_off,
        "one_off_share": (one_off / len(senders)) if senders else None,
        "buyer_fanout": len(amounts), "median_to_buyers": amounts[len(amounts) // 2] if amounts else None,
    }


def oracle_hits(node: dict, measures: dict, thresholds: dict, lists: dict | None, ingest_threshold: int) -> list[dict]:
    """Критерії хаба для вершини буквально за порогами (`>`; пил — `<`; передумови `>=`)."""
    hits = []
    if lists is not None:
        for category, addresses in lists["categories"].items():
            if node["address"] in addresses:
                hits.append({"criterion": "known_list", "measured": None, "threshold": None,
                             "detail": f"list:{category}", "lists_version": lists["version"]})
    if thresholds["prune_off_curve"] and node["address_type"] == "off_curve":
        hits.append({"criterion": "known_list", "measured": None, "threshold": None,
                     "detail": "address_type:off_curve", "lists_version": None})
    if measures["degree"] > thresholds["degree_threshold"]:
        hits.append({"criterion": "degree", "measured": measures["degree"],
                     "threshold": thresholds["degree_threshold"], "detail": "measured", "lists_version": None})
    if (measures["buyer_fanout"] >= thresholds["dust_min_fanout"]
            and measures["median_to_buyers"] < thresholds["dust_amount_lamports"]):
        hits.append({"criterion": "dust_fanout", "measured": measures["median_to_buyers"],
                     "threshold": thresholds["dust_amount_lamports"], "detail": "measured", "lists_version": None})
    if (measures["unique_senders"] >= thresholds["one_off_min_senders"]
            and measures["one_off_share"] > thresholds["one_off_senders_share"]):
        hits.append({"criterion": "one_off_senders", "measured": measures["one_off_share"],
                     "threshold": thresholds["one_off_senders_share"], "detail": "measured", "lists_version": None})
    mark = node["unexpanded"]
    if thresholds["prune_ingest_high_degree"] and mark is not None and mark["reason"] == "high_degree":
        hits.append({"criterion": "ingest_high_degree", "measured": mark["counterparties_seen"],
                     "threshold": ingest_threshold, "detail": "unexpanded:high_degree", "lists_version": None})
    hits.sort(key=lambda h: (h["criterion"], h["detail"]))
    return hits


def bfs_components(addresses: list[str], edges: list[dict]) -> list[list[str]]:
    """Слабкі компоненти обходом у ширину по ненапрямленому вигляду графа."""
    adjacency: dict[str, set[str]] = {a: set() for a in addresses}
    for e in edges:
        adjacency[e["sender"]].add(e["receiver"])
        adjacency[e["receiver"]].add(e["sender"])
    seen: set[str] = set()
    out = []
    for start in sorted(addresses):
        if start in seen:
            continue
        seen.add(start)
        queue, members = deque([start]), []
        while queue:
            current = queue.popleft()
            members.append(current)
            for nxt in sorted(adjacency[current]):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        out.append(sorted(members))
    return out


def _snapshot(addresses: list[str], edges: list[dict], buyer_set: set[str]) -> tuple[dict, list[dict]]:
    comps = bfs_components(addresses, edges)
    listed = [{"id": min(c), "members": c, "buyers": sum(1 for m in c if m in buyer_set)} for c in comps]
    listed.sort(key=lambda c: c["id"])
    total = len(buyer_set)
    best = max((c["buyers"] for c in listed), default=0)
    touched = {e["sender"] for e in edges} | {e["receiver"] for e in edges}
    snap = {
        "nodes": len(addresses), "edges": len(edges), "components": len(comps), "buyers_total": total,
        "buyers_in_largest_component": best,
        "largest_component_buyer_share": (best / total) if total else 0.0,
        "isolated_buyers": sum(1 for b in buyer_set if b not in touched),
    }
    return snap, listed


def oracle_depths(addresses: list[str], buyer_set: set[str], transfers: list[dict]) -> dict[str, int]:
    """Мінімальна глибина за правилами R-6: покупець 0; отримувач переказу <= depth - 1; відправник <= depth."""
    depth = {a: (0 if a in buyer_set else 99) for a in addresses}
    for t in transfers:
        depth[t["receiver"]] = min(depth[t["receiver"]], t["depth"] - 1)
        depth[t["sender"]] = min(depth[t["sender"]], t["depth"])
    return depth


def oracle_warnings(before: dict, after: dict, thresholds: dict, *, lists_applied: bool,
                    sources_before: int, sources_after: int) -> list[str]:
    """Попередження звіту (R-10, R-16): гігантська компонента лише при частці СТРОГО більше порогу."""
    warnings = []
    if after["largest_component_buyer_share"] > thresholds["giant_component_warn_share"]:
        warnings.append("giant_component")
    if before["buyers_total"] == 0:
        warnings.append("empty_graph")
    if not lists_applied:
        warnings.append("address_lists_not_applied")
    if sources_before > 0 and sources_after == 0:
        warnings.append("all_sources_pruned")
    return sorted(warnings)


def build_expected(s: Scenario, ingest: dict) -> dict:
    th, lists, ct = s.thresholds, s.lists, ingest["metadata"]["counterparty_threshold"]
    buyers = {b["wallet"]: b for b in ingest["buyers"]}
    unexpanded = {u["wallet"]: {k: u[k] for k in ("reason", "counterparties_seen", "signatures_seen",
                                                  "signatures_truncated")} for u in ingest["unexpanded"]}
    edges = aggregate_edges(ingest["transfers"])
    addresses = sorted(set(buyers) | {t["sender"] for t in ingest["transfers"]}
                       | {t["receiver"] for t in ingest["transfers"]})

    # --- вершини: ролі й глибина за правилами R-6 ---
    senders_of_any = {t["sender"] for t in ingest["transfers"]}
    depth = oracle_depths(addresses, set(buyers), ingest["transfers"])
    nodes = []
    for a in addresses:
        roles = sorted(({"buyer"} if a in buyers else set()) | ({"funder"} if a in senders_of_any else set()))
        if a in buyers:
            address_type = buyers[a]["address_type"]
        else:
            address_type = "wallet" if Pubkey.from_string(a).is_on_curve() else "off_curve"
        nodes.append({
            "address": a, "roles": roles, "depth": depth[a],
            "buyer_rank": buyers[a]["rank"] if a in buyers else None,
            "address_type": address_type, "unexpanded": unexpanded.get(a),
            "measures": oracle_measures(a, edges, set(buyers)),
        })
    by_address = {n["address"]: n for n in nodes}

    # --- звірка оракула з оголошеними ролями й глибинами ---
    label_of = {address: label for label, address in s.wallets.items()}
    declared = {s.a(label): v for label, v in s.vertices.items()}
    assert set(declared) == set(addresses), (
        f"{s.name}: оголошені вершини != вершини з переказів: "
        f"{sorted(label_of.get(a, a) for a in set(declared) ^ set(addresses))}")
    for a, (roles, d) in declared.items():
        assert (tuple(by_address[a]["roles"]), by_address[a]["depth"]) == (roles, d), (
            f"{s.name}: {label_of[a]} оголошено {roles}/{d}, оракул дав "
            f"{tuple(by_address[a]['roles'])}/{by_address[a]['depth']}")

    # --- відсікання ---
    records, flags = [], []
    for n in nodes:
        hits = oracle_hits(n, n["measures"], th, lists, ct)
        if not hits:
            continue
        if n["buyer_rank"] is None:
            incident = [e for e in edges if n["address"] in (e["sender"], e["receiver"])]
            records.append({
                "address": n["address"], "criteria": hits, "incident_edges": incident, "measures": n["measures"],
                "config_version": CONFIG_VERSION, "lists_version": lists["version"] if lists is not None else None,
            })
        else:
            flags.append({"address": n["address"], "buyer_rank": n["buyer_rank"], "criteria": hits,
                          "measures": n["measures"]})
    flags.sort(key=lambda f: f["buyer_rank"])

    def names(items):
        return {label_of[i["address"]]: tuple(f"{h['criterion']}:{h['detail']}" for h in i["criteria"])
                for i in items}

    assert names(records) == {k: tuple(sorted(v)) for k, v in s.hubs.items()}, (
        f"{s.name}: оракул відсікання {names(records)} != оголошені хаби {s.hubs}")
    assert names(flags) == {k: tuple(sorted(v)) for k, v in s.flags.items()}, (
        f"{s.name}: оракул позначок {names(flags)} != оголошені {s.flags}")

    hubs = {r["address"] for r in records}
    after_nodes = [a for a in addresses if a not in hubs]
    after_edges = [e for e in edges if e["sender"] not in hubs and e["receiver"] not in hubs]
    buyer_set = set(buyers)
    before, before_components = _snapshot(addresses, edges, buyer_set)
    after, after_components = _snapshot(after_nodes, after_edges, buyer_set)
    assert buyer_set <= set(after_nodes)

    warnings = oracle_warnings(
        before, after, th, lists_applied=lists is not None,
        sources_before=sum(1 for n in nodes if "buyer" not in n["roles"]),
        sources_after=sum(1 for a in after_nodes if a not in buyers))

    missing = ingest["completeness"]["missing"]
    return {
        "scenario": s.name, "description": s.description, "mint": ingest["metadata"]["mint"],
        "wallets": dict(sorted(s.wallets.items())),
        "config": {"version": CONFIG_VERSION, "thresholds": th, "lists": lists},
        "ingest_counterparty_threshold": ct,
        "completeness": {
            "status": "complete" if ingest["completeness"]["status"] == "complete" else "incomplete",
            "ingest_status": ingest["completeness"]["status"], "missing": missing,
            "buyers_complete": ingest["completeness"]["buyers"]["complete"],
            "buyers_reason": ingest["completeness"]["buyers"]["reason"],
        },
        "graph": {"nodes": nodes, "edges": edges},
        "prune": {
            "lists_applied": lists is not None, "config_version": CONFIG_VERSION,
            "lists_version": lists["version"] if lists is not None else None,
            "records": records, "buyer_flags": flags,
            "after": {"node_addresses": after_nodes, "edge_keys": [list(edge_key(e)) for e in after_edges]},
        },
        "components": {"before": before_components, "after": after_components},
        "report": {
            "before": before, "after": after, "pruned_nodes": len(hubs),
            "pruned_edges": len(edges) - len(after_edges), "warn_share": th["giant_component_warn_share"],
            "warnings": warnings,
        },
        "notes": {
            "criteria_order": "хити впорядковано за (criterion, detail) як рядками: degree < dust_fanout < ingest_high_degree < known_list < one_off_senders",
            "edge_asset_key": "ключ ребра в edge_keys: [kind, sender, receiver, asset або \"\"]",
            "warnings": "без delegated_incomplete: поле delegated у результаті 001 з'явиться з T-044 (g_delegated — T-047)",
            "graph_completeness": "лише частина з результату збору (ingest_status, missing, buyers_*); delegated_* — T-044",
        },
    }


# ---------------------------------------------------------------------------------------
# Сценарії
# ---------------------------------------------------------------------------------------


def g_basic() -> Scenario:
    s = Scenario("g_basic", "5 покупців; A->P1, A->P2; B->A; C->B; цикл A<->D; покупець P3 фінансує покупця P4 "
                            "(вершина з двома ролями); три перекази A->P2 в SOL (два в одній транзакції) -> одне ребро; "
                            "SOL і spl:USDC між A і P2 -> два ребра; P5 без фінансування.", first_buyers_n=5)
    s.wallet("USDC")
    for label in ("A", "B", "C", "D", "P1", "P2", "P3", "P4", "P5"):
        s.wallet(label)
    s.buy("P1", 200, spent=[("sol", 600_000_000)], received=4_000_000_000)
    s.buy("P2", 210, spent=[("sol", 700_000_000), ("spl:USDC", 5_000_000)], received=3_000_000_000)
    s.buy("P3", 220, spent=[("sol", 500_000_000)], received=2_000_000_000)
    s.buy("P4", 230, spent=[("sol", 400_000_000)], received=1_500_000_000)
    s.buy("P5", 240, spent=[("sol", 300_000_000)], received=1_000_000_000)
    s.transfer("t_cb", "0", "C", "B", 100, 5 * SOL, 3)
    s.transfer("t_ad", "0", "A", "D", 104, 1 * SOL, 3)
    s.transfer("t_da", "0", "D", "A", 108, 1_500_000_000, 2)
    s.transfer("t_ba", "0", "B", "A", 110, 4 * SOL, 2)
    s.transfer("t_ap1", "0.0", "A", "P1", 120, 3 * SOL, 1)
    s.transfer("t_ap2", "0", "A", "P2", 125, 1 * SOL, 1)
    s.transfer("t_ap2", "1", "A", "P2", 125, 1 * SOL, 1)
    s.transfer("t_usdc", "0", "A", "P2", 126, 25_000_000, 1, asset="spl:USDC", decimals=6)
    s.transfer("t_p3p4", "0", "P3", "P4", 128, 2 * SOL, 1)
    s.transfer("t_ap2b", "0", "A", "P2", 130, 2 * SOL, 1)
    s.declare(["A"], ("funder",), 1)
    s.declare(["B", "D"], ("funder",), 2)
    s.declare(["C"], ("funder",), 3)
    s.declare(["P1", "P2", "P4", "P5"], ("buyer",), 0)
    s.declare(["P3"], ("buyer", "funder"), 0)
    return s


def g_hub() -> Scenario:
    s = Scenario("g_hub", "20 покупців; хаб H фінансує 18 і має 32 відправники (30 одноразових, S31 двічі в SOL, S32 по "
                          "одному разу в SOL і spl:USDC — разом 2, тож теж не одноразовий); "
                          "F фінансує 3 покупців (B01, B19, B20) і лишається; до відсікання покупці склеєні в одну "
                          "компоненту, після — ні. H відсікається лише за частотою одноразових відправників.",
                 first_buyers_n=20)
    s.wallet("USDC")
    s.wallet("H")
    s.wallet("F")
    buyers = [f"B{i:02d}" for i in range(1, 21)]
    senders = [f"S{i:02d}" for i in range(1, 33)]
    for label in buyers + senders:
        s.wallet(label)
    for i, label in enumerate(buyers):
        s.buy(label, 200 + 10 * i, spent=[("sol", 500_000_000 + i)], received=1_000_000_000 + i)
    for i in range(1, 19):
        s.transfer(f"h_b{i:02d}", "0", "H", f"B{i:02d}", 100 + i, 1 * SOL, 1)
    s.transfer("h_b01_usdc", "0", "H", "B01", 150, 10_000_000, 1, asset="spl:USDC", decimals=6)
    s.transfer("f_b01", "0", "F", "B01", 140, 2 * SOL, 1)
    s.transfer("f_b19", "0", "F", "B19", 141, 2 * SOL, 1)
    s.transfer("f_b20", "0", "F", "B20", 142, 2 * SOL, 1)
    for i in range(1, 31):
        s.transfer(f"s{i:02d}_h", "0", f"S{i:02d}", "H", 10 + i, SOL // 2, 2)
    s.transfer("s31_h_a", "0", "S31", "H", 41, SOL // 2, 2)
    s.transfer("s31_h_b", "0", "S31", "H", 42, SOL // 2, 2)
    s.transfer("s32_h_sol", "0", "S32", "H", 43, SOL // 2, 2)
    s.transfer("s32_h_usdc", "0", "S32", "H", 44, 20_000_000, 2, asset="spl:USDC", decimals=6)
    s.declare(["H", "F"], ("funder",), 1)
    s.declare(senders, ("funder",), 2)
    s.declare(buyers, ("buyer",), 0)
    s.hubs["H"] = ("one_off_senders:measured",)
    return s


def g_known() -> Scenario:
    s = Scenario("g_known", "Джерело зі списку launchpads (Pump.fun), джерело-PDA (off-curve) і звичайний "
                            "фінансувальник F1; списки застосовано, exchanges і market_makers порожні.",
                 first_buyers_n=4)
    s.fixed("PUMP", PUMP)
    s.pda("PDA_SRC")
    for label in ("F1", "P1", "P2", "P3", "P4"):
        s.wallet(label)
    for i, label in enumerate(("P1", "P2", "P3", "P4")):
        s.buy(label, 200 + 10 * i, spent=[("sol", 400_000_000)], received=1_000_000_000)
    s.transfer("pump_p1", "0", "PUMP", "P1", 100, 1 * SOL, 1)
    s.transfer("pump_p2", "0", "PUMP", "P2", 101, 1 * SOL, 1)
    s.transfer("pda_p3", "0", "PDA_SRC", "P3", 102, 1 * SOL, 1)
    s.transfer("f1_p4", "0", "F1", "P4", 103, 1 * SOL, 1)
    s.declare(["PUMP", "PDA_SRC", "F1"], ("funder",), 1)
    s.declare(["P1", "P2", "P3", "P4"], ("buyer",), 0)
    s.hubs["PUMP"] = ("known_list:list:launchpads",)
    s.hubs["PDA_SRC"] = ("known_list:address_type:off_curve",)
    return s


def g_buyer_hub() -> Scenario:
    s = Scenario("g_buyer_hub", "Покупець X (PDA) має 12 одноразових відправників, фінансує 89 інших покупців "
                                "(ступінь 101) і позначений збором high_degree; усім 89 надсилає по 500 000 лампортів "
                                "(пил, медіана < 0,001 SOL): задовольняє всі п'ять критеріїв, але лишається — "
                                "отримує лише позначку.",
                 first_buyers_n=100, counterparty_threshold=12)
    s.pda("X")
    senders = [f"S{i:02d}" for i in range(1, 13)]
    ys = [f"Y{i:02d}" for i in range(1, 90)]
    for label in senders + ys:
        s.wallet(label)
    s.buy("X", 200, spent=[("sol", 800_000_000)], received=5_000_000_000)
    for i, label in enumerate(ys, start=1):
        s.buy(label, 200 + i, spent=[("sol", 300_000_000)], received=1_000_000_000)
    for i, label in enumerate(senders, start=1):
        s.transfer(f"{label.lower()}_x", "0", label, "X", 10 + i, 1 * SOL, 1)
    for i, label in enumerate(ys, start=1):
        s.transfer(f"x_{label.lower()}", "0", "X", label, 100 + i, 500_000, 1)
    s.unexpanded.append(dict(wallet="X", depth=0, reason="high_degree", counterparties_seen=13,
                             signatures_seen=13, signatures_truncated=False))
    s.declare(["X"], ("buyer", "funder"), 0)
    s.declare(senders, ("funder",), 1)
    s.declare(ys, ("buyer",), 0)
    s.flags["X"] = ("degree:measured", "dust_fanout:measured", "ingest_high_degree:unexpanded:high_degree",
                    "known_list:address_type:off_curve", "one_off_senders:measured")
    return s


def g_incomplete() -> Scenario:
    s = Scenario("g_incomplete", "Збір неповний: історія гаманця B не отримана (timeout), хоча перелік покупців "
                                 "повний (buyers.complete=true) — єдиний запис missing робить результат incomplete.",
                 first_buyers_n=3)
    for label in ("A", "B", "P1", "P2", "P3"):
        s.wallet(label)
    for i, label in enumerate(("P1", "P2", "P3")):
        s.buy(label, 200 + 10 * i, spent=[("sol", 400_000_000)], received=1_000_000_000)
    s.transfer("a_p1", "0", "A", "P1", 100, 1 * SOL, 1)
    s.transfer("b_p2", "0", "B", "P2", 101, 1 * SOL, 1)
    s.missing.append(dict(wallet="B", depth=1, reason="timeout", detail="getSignaturesForAddress: timeout after 2 retries"))
    s.declare(["A", "B"], ("funder",), 1)
    s.declare(["P1", "P2", "P3"], ("buyer",), 0)
    return s


def g_empty() -> Scenario:
    return Scenario("g_empty", "Токен без покупців: порожній результат збору (повний) — порожній граф, не помилка.",
                    first_buyers_n=5)


def g_all_hubs() -> Scenario:
    s = Scenario("g_all_hubs", "Кожне не-покупець-джерело відповідає рівно одному критерію: S_LIST (список "
                               "launchpads), S_PDA (off-curve), S_DEG (ступінь 4 > 3), S_UNEXP (позначка збору "
                               "high_degree). Пороги тут малі: degree_threshold=3, counterparty_threshold=2.",
                 first_buyers_n=6, counterparty_threshold=2, thresholds={"degree_threshold": 3})
    s.fixed("S_LIST", PUMP)
    s.pda("S_PDA")
    s.wallet("S_DEG")
    s.wallet("S_UNEXP")
    buyers = [f"B{i}" for i in range(1, 7)]
    for label in buyers:
        s.wallet(label)
    for i, label in enumerate(buyers):
        s.buy(label, 200 + 10 * i, spent=[("sol", 400_000_000)], received=1_000_000_000)
    s.transfer("list_b1", "0", "S_LIST", "B1", 100, 1 * SOL, 1)
    s.transfer("pda_b2", "0", "S_PDA", "B2", 101, 1 * SOL, 1)
    for i in (3, 4, 5, 6):
        s.transfer(f"deg_b{i}", "0", "S_DEG", f"B{i}", 102 + i, 1 * SOL, 1)
    s.transfer("unexp_b3", "0", "S_UNEXP", "B3", 110, 1 * SOL, 1)
    s.transfer("b1_unexp", "0", "B1", "S_UNEXP", 90, SOL // 2, 2)
    s.transfer("b2_unexp", "0", "B2", "S_UNEXP", 91, SOL // 2, 2)
    s.unexpanded.append(dict(wallet="S_UNEXP", depth=1, reason="high_degree", counterparties_seen=3,
                             signatures_seen=3, signatures_truncated=False))
    s.declare(["S_LIST", "S_PDA", "S_DEG", "S_UNEXP"], ("funder",), 1)
    s.declare(["B1", "B2"], ("buyer", "funder"), 0)
    s.declare(["B3", "B4", "B5", "B6"], ("buyer",), 0)
    s.hubs["S_LIST"] = ("known_list:list:launchpads",)
    s.hubs["S_PDA"] = ("known_list:address_type:off_curve",)
    s.hubs["S_DEG"] = ("degree:measured",)
    s.hubs["S_UNEXP"] = ("ingest_high_degree:unexpanded:high_degree",)
    return s


def g_unexpanded() -> Scenario:
    s = Scenario("g_unexpanded", "Одна вершина UH нерозгорнута через high_degree (відсікається), одна US — через "
                                 "signature_cap (лишається): другий вид не є критерієм хаба.",
                 first_buyers_n=3, counterparty_threshold=3)
    for label in ("UH", "US", "X1", "X2", "X3", "Y1", "Y2", "P1", "P2", "P3"):
        s.wallet(label)
    for i, label in enumerate(("P1", "P2", "P3")):
        s.buy(label, 200 + 10 * i, spent=[("sol", 400_000_000)], received=1_000_000_000)
    s.transfer("uh_p1", "0", "UH", "P1", 100, 1 * SOL, 1)
    s.transfer("uh_p2", "0", "UH", "P2", 101, 1 * SOL, 1)
    s.transfer("us_p3", "0", "US", "P3", 102, 1 * SOL, 1)
    for i, label in enumerate(("X1", "X2", "X3"), start=1):
        s.transfer(f"{label.lower()}_uh", "0", label, "UH", 20 + i, SOL // 2, 2)
    for i, label in enumerate(("Y1", "Y2"), start=1):
        s.transfer(f"{label.lower()}_us", "0", label, "US", 30 + i, SOL // 2, 2)
    s.unexpanded.append(dict(wallet="UH", depth=1, reason="high_degree", counterparties_seen=4,
                             signatures_seen=4, signatures_truncated=False))
    s.unexpanded.append(dict(wallet="US", depth=1, reason="signature_cap", counterparties_seen=2,
                             signatures_seen=300, signatures_truncated=True))
    s.declare(["UH", "US"], ("funder",), 1)
    s.declare(["X1", "X2", "X3", "Y1", "Y2"], ("funder",), 2)
    s.declare(["P1", "P2", "P3"], ("buyer",), 0)
    s.hubs["UH"] = ("ingest_high_degree:unexpanded:high_degree",)
    return s


def g_dust() -> Scenario:
    s = Scenario("g_dust", "20 покупців; пилове джерело D фінансує B01…B19: 18 × 500 000 лампортів і B19 — 5 SOL (один "
                           "великий переказ серед пилу медіану не зрушує), має 2 відправники (ступінь 21, one_off не "
                           "застосовний) -> відсікається лише за dust_fanout; E фінансує B02…B05 пилом — fan-out 4 = "
                           "поріг − 1, критерій не застосовний, лишається; F фінансує B01, B19, B20 по 2 SOL і лишається; "
                           "до відсікання частка покупців у найбільшій компоненті >= 0.9, після — 0.2 < 0.5.",
                 first_buyers_n=20)
    for label in ("D", "E", "F", "S1", "S2"):
        s.wallet(label)
    buyers = [f"B{i:02d}" for i in range(1, 21)]
    for label in buyers:
        s.wallet(label)
    for i, label in enumerate(buyers):
        s.buy(label, 200 + 10 * i, spent=[("sol", 500_000_000 + i)], received=1_000_000_000 + i)
    for i in range(1, 19):
        s.transfer(f"d_b{i:02d}", "0", "D", f"B{i:02d}", 100 + i, 500_000, 1)
    s.transfer("d_b19", "0", "D", "B19", 119, 5 * SOL, 1)
    for i in range(2, 6):
        s.transfer(f"e_b{i:02d}", "0", "E", f"B{i:02d}", 130 + i, 500_000, 1)
    for i, label in ((1, "B01"), (2, "B19"), (3, "B20")):
        s.transfer(f"f_{label.lower()}", "0", "F", label, 140 + i, 2 * SOL, 1)
    s.transfer("s1_d", "0", "S1", "D", 11, SOL // 2, 2)
    s.transfer("s2_d", "0", "S2", "D", 12, SOL // 2, 2)
    s.declare(["D", "E", "F"], ("funder",), 1)
    s.declare(["S1", "S2"], ("funder",), 2)
    s.declare(buyers, ("buyer",), 0)
    s.hubs["D"] = ("dust_fanout:measured",)
    return s


def g_financier() -> Scenario:
    s = Scenario("g_financier", "30 покупців; єдиний справжній фінансист R: B01…B29 по 700 000 000 лампортів і B30 — "
                                "400 000 (один пиловий серед справжніх); R має 2 відправники -> ступінь 32 (>= 30 і < 100). "
                                "Нічого не відсікається; звіт до == після, giant_component у попередженнях — чесно: один "
                                "справжній фінансист усіх покупців — сигнал для кластеризації (003), а не хаб.",
                 first_buyers_n=30)
    for label in ("R", "S1", "S2"):
        s.wallet(label)
    buyers = [f"B{i:02d}" for i in range(1, 31)]
    for label in buyers:
        s.wallet(label)
    for i, label in enumerate(buyers):
        s.buy(label, 200 + 10 * i, spent=[("sol", 500_000_000 + i)], received=1_000_000_000 + i)
    for i in range(1, 30):
        s.transfer(f"r_b{i:02d}", "0", "R", f"B{i:02d}", 100 + i, 700_000_000, 1)
    s.transfer("r_b30", "0", "R", "B30", 130, 400_000, 1)
    s.transfer("s1_r", "0", "S1", "R", 11, SOL // 2, 2)
    s.transfer("s2_r", "0", "S2", "R", 12, SOL // 2, 2)
    s.declare(["R"], ("funder",), 1)
    s.declare(["S1", "S2"], ("funder",), 2)
    s.declare(buyers, ("buyer",), 0)
    return s


def g_dust_mixed() -> Scenario:
    s = Scenario("g_dust_mixed", "10 покупців; змішані відправники й межі порогу пилу. X -> 4 пилових + 3 x 500 000 000 "
                                 "(n = 7, верхня медіана — пил -> хаб); Y -> 3 пилових + 3 справжніх (n = 6 -> справжня "
                                 "-> не хаб); W -> 5 x 999 999 (поріг − 1 -> хаб); Z -> 5 x 1 000 000 (рівно поріг -> не "
                                 "хаб); V -> 5 x 1 000 001 (не хаб); U -> 5 покупців лише в spl:USDC (fan-out 0 — "
                                 "критерій не застосовний).", first_buyers_n=10)
    s.wallet("USDC")
    for label in ("X", "Y", "W", "Z", "V", "U"):
        s.wallet(label)
    buyers = [f"B{i:02d}" for i in range(1, 11)]
    for label in buyers:
        s.wallet(label)
    for i, label in enumerate(buyers):
        s.buy(label, 200 + 10 * i, spent=[("sol", 500_000_000 + i)], received=1_000_000_000 + i)
    slot = iter(range(100, 1000))

    def fund(source: str, targets: range, amount: int) -> None:
        for i in targets:
            s.transfer(f"{source.lower()}_b{i:02d}", "0", source, f"B{i:02d}", next(slot), amount, 1)

    fund("X", range(1, 5), 500_000)
    fund("X", range(5, 8), 500_000_000)
    fund("Y", range(1, 4), 500_000)
    fund("Y", range(8, 11), 500_000_000)
    fund("W", range(1, 6), 999_999)
    fund("Z", range(1, 6), 1_000_000)
    fund("V", range(1, 6), 1_000_001)
    for i in range(6, 11):
        s.transfer(f"u_b{i:02d}", "0", "U", f"B{i:02d}", next(slot), 10_000_000, 1, asset="spl:USDC", decimals=6)
    s.declare(["X", "Y", "W", "Z", "V", "U"], ("funder",), 1)
    s.declare(buyers, ("buyer",), 0)
    s.hubs["X"] = ("dust_fanout:measured",)
    s.hubs["W"] = ("dust_fanout:measured",)
    return s


SCENARIO_BUILDERS = {
    "g_basic": g_basic, "g_hub": g_hub, "g_known": g_known, "g_buyer_hub": g_buyer_hub,
    "g_incomplete": g_incomplete, "g_empty": g_empty, "g_all_hubs": g_all_hubs, "g_unexpanded": g_unexpanded,
    "g_dust": g_dust, "g_financier": g_financier, "g_dust_mixed": g_dust_mixed,
}
assert tuple(SCENARIO_BUILDERS) == SCENARIO_NAMES


# ---------------------------------------------------------------------------------------
# Серіалізація й запис
# ---------------------------------------------------------------------------------------


def _compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fmt(value, indent: int = 0) -> str:
    """Об'єкти розгорнуті, масиви об'єктів — по одному елементу на рядок: діфи читаються."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f'{pad}  {json.dumps(k, ensure_ascii=False)}: {_fmt(v, indent + 2)}' for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value or all(not isinstance(v, (dict, list)) for v in value):
            return _compact(value)
        return "[\n" + ",\n".join(f"{pad}  {_compact(v)}" for v in value) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def dump(data) -> str:
    text = _fmt(data) + "\n"
    assert json.loads(text) == data
    return text


def build_all() -> dict[str, str]:
    """Повертає {відносний шлях: текст} для всіх згенерованих файлів."""
    out = {}
    for name in SCENARIO_NAMES:
        scenario = SCENARIO_BUILDERS[name]()
        ingest = build_ingest(scenario)
        out[f"{name}/ingest.json"] = dump(ingest)
        out[f"{name}/expected.json"] = dump(build_expected(scenario, ingest))
    return out


def main(argv: list[str]) -> int:
    built = build_all()
    if "--check" in argv:
        stale = [rel for rel, text in built.items()
                 if not (OUT_DIR / rel).exists() or (OUT_DIR / rel).read_text(encoding="utf-8") != text]
        existing = ({p.relative_to(OUT_DIR).as_posix() for p in OUT_DIR.rglob("*") if p.is_file()}
                    if OUT_DIR.exists() else set())
        extra = sorted(existing - set(built))
        if stale:
            print("stale:", ", ".join(stale))
        if extra:
            print("extra:", ", ".join(extra))
        return 1 if stale or extra else 0
    for rel, text in built.items():
        path = OUT_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
