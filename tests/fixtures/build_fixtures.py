# trace: ignore-file
"""Генератор синтетичних сценаріїв у формі відповідей Solana JSON-RPC.

Запуск: `uv run python tests/fixtures/build_fixtures.py [--check]` (без аргументів перезаписує
`tests/fixtures/scenarios/{basic,hub,corrupt,notfound}`; `--check` лише порівнює з диском).

Принципи
--------
* Не імпортує `unmask`: генератор — незалежний від коду збору еталон. Використовує лише
  stdlib і `solders` (справжні base58-адреси, ключі на кривій ed25519 і PDA поза кривою).
* Детермінований: адреси й підписи виводяться з міток через sha256/sha512, жодного
  `random`/`time`; дві збірки поспіль збігаються побайтно.
* Сценарій описується декларативно (таблиці ребер із позначкою «включити з глибиною d» або
  «виключити з причиною»). `expected.json` складається з цих позначок, а НЕ з обходу
  `rpc.json` алгоритмом збору, тож він лишається еталоном для перевірки реалізації.
* Реєстр лампортів і токен-рахунків веде самосуміжний стан: у кожній транзакції
  `sum(pre) - fee == sum(post)`, баланси не йдуть у мінус, `pre/postTokenBalances`
  узгоджені з інструкціями.

Правила еталона (research.md R-1, R-3, R-6, R-8, R-9; рішення Q1 — причинне відсікання)
---------------------------------------------------------------------------------------
* Покупець (глибина 0) — гаманець, що отримав mint в обмін на інший актив (R-2).
  Порядок: (слот першої купівлі, підпис, гаманець).
* Переказ у вершину X береться, якщо він відбувся строго раніше за межу X: для покупця —
  його перша купівля; для вершини, досягнутої ребром на рівні d-1, — найпізніше з ребер,
  що привели в неї на цьому рівні (ребра, відкриті на глибших рівнях, межу вже
  розгорнутої вершини не змінюють).
* `depth` переказу = глибина вершини-отримувача + 1, мінімальна з усіх шляхів. Не глибше
  `funding_depth`. Вершини-відправники, уже розгорнуті на меншій глибині, повторно не
  розгортаються; сам переказ при цьому записується (цикл A->D->A).
* Не входять: перекази після межі, невдалі транзакції, самопереводи, переказ у вершину,
  розгортання якої зупинене порогом, та все, що лежить за межею глибини.
* Порогові випадки хаба (R-3): сканування від найновішого запису до межі. Поріг
  контрагентів перевищено, коли унікальних відправників стало БІЛЬШЕ порога; переказ
  відправника, що перевищив поріг, НЕ збирається (`понад поріг не збираються`), а
  `counterparties_seen` = порогове значення + 1. Ліміт підписів: переглядається рівно
  `max_signatures_per_wallet` найновіших записів до межі.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

SOL = 10**9
FEE = 5000
ATA_RENT = 2_039_280
MINT_RENT = 1_461_600
BASE_TIME = 1_759_400_000
INSTRUCTION_ERROR = {"InstructionError": [0, {"Custom": 1}]}  # недостатньо коштів


# ---------------------------------------------------------------------------------------
# Адреси, підписи, форматування
# ---------------------------------------------------------------------------------------


def _digest(*parts: str, algo: str = "sha256") -> bytes:
    return hashlib.new(algo, "/".join(("unmask-fixture",) + parts).encode()).digest()


def ui_amount(amount: int, decimals: int) -> dict:
    exact = Decimal(amount).scaleb(-decimals)
    text = format(exact.normalize(), "f") if amount else "0"
    return {
        "amount": str(amount),
        "decimals": decimals,
        "uiAmount": (amount / 10**decimals) if amount else None,
        "uiAmountString": text,
    }


def ata_address(owner: str, mint: str) -> str:
    seeds = [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(TOKEN)), bytes(Pubkey.from_string(mint))]
    return str(Pubkey.find_program_address(seeds, Pubkey.from_string(ATA_PROGRAM))[0])


def parsed_ix(program: str, program_id: str, kind: str, info: dict) -> dict:
    return {"program": program, "programId": program_id, "parsed": {"info": dict(sorted(info.items())), "type": kind}}


# ---------------------------------------------------------------------------------------
# Світ: реєстр лампортів, токен-рахунків і записаних транзакцій
# ---------------------------------------------------------------------------------------


class World:
    def __init__(self, scenario: str, description: str) -> None:
        self.scenario = scenario
        self.description = description
        self.cast: dict[str, str] = {}
        self.first_seen: dict[str, int] = {}
        self.untracked: set[str] = set()
        self.programs: set[str] = set()
        self.lamports: dict[str, int] = {}
        self.tokens: dict[str, dict] = {}
        self.mints: dict[str, int] = {}
        self.txs: dict[str, dict] = {}
        self.tx_order: list[str] = []
        self.labels: dict[str, tuple[str, int]] = {}
        self.slot_of: dict[str, int] = {}
        self.err_of: dict[str, object] = {}
        self.history: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        self._events: list[tuple[int, int, object, tuple]] = []
        self._seq = 0
        for program in (SYSTEM, TOKEN, ATA_PROGRAM):
            self._register_program(program)

    # --- адреси ---
    def _see(self, address: str) -> None:
        self.first_seen.setdefault(address, len(self.first_seen))

    def wallet(self, label: str, *, lamports: int = 0, tracked: bool = True) -> str:
        key = Keypair.from_seed(_digest(self.scenario, "wallet", label))
        address = str(key.pubkey())
        self.cast[label] = address
        self._see(address)
        if lamports:
            self.lamports[address] = lamports
        if not tracked:
            self.untracked.add(address)
        return address

    def _register_program(self, address: str) -> str:
        self.programs.add(address)
        self.lamports[address] = 1  # програми в preBalances/postBalances мають ненульовий баланс
        return address

    def program(self, label: str) -> str:
        address = str(Keypair.from_seed(_digest(self.scenario, "program", label)).pubkey())
        self.cast[label] = address
        return self._register_program(address)

    def pda(self, label: str, seeds: list[bytes], program: str, *, lamports: int = 0) -> str:
        address = str(Pubkey.find_program_address(seeds, Pubkey.from_string(program))[0])
        assert not Pubkey.from_string(address).is_on_curve()
        self.cast[label] = address
        self._see(address)
        if lamports:
            self.lamports[address] = lamports
        return address

    def mint(self, label: str, decimals: int, *, genesis: bool = False) -> str:
        """`genesis=True` — mint існує від початку; інакше його створює транзакція сценарію."""
        address = self.wallet(label, lamports=MINT_RENT if genesis else 0)
        self.mints[address] = decimals
        return address

    def genesis_token_account(self, owner: str, mint: str, amount: int) -> str:
        address = ata_address(owner, mint)
        self.tokens[address] = {"owner": owner, "mint": mint, "amount": amount}
        self.lamports[address] = ATA_RENT
        self._see(address)
        return address

    def signature(self, label: str) -> str:
        return str(Signature.from_bytes(_digest(self.scenario, "tx", label, algo="sha512")))

    def sig_of(self, label: str) -> str:
        return self.labels[label][0]

    # --- розклад подій у порядку слотів ---
    def at(self, slot: int, fn, *args) -> None:
        self._events.append((slot, self._seq, fn, args))
        self._seq += 1

    def run(self) -> None:
        for _, _, fn, args in sorted(self._events, key=lambda e: (e[0], e[1])):
            fn(*args)
        self._events = []

    # --- виходи ---
    def account_info_for_mint(self, mint: str) -> dict:
        supply = sum(t["amount"] for t in self.tokens.values() if t["mint"] == mint)
        return {
            "lamports": MINT_RENT, "owner": TOKEN, "executable": False, "rentEpoch": 0, "space": 82,
            "data": {"program": "spl-token", "space": 82, "parsed": {"type": "mint", "info": {
                "decimals": self.mints[mint], "supply": str(supply), "isInitialized": True,
                "mintAuthority": None, "freezeAuthority": None}}},
        }

    def token_accounts_by_owner(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for address in sorted(self.tokens, key=lambda a: self.first_seen[a]):
            t = self.tokens[address]
            info = {
                "isNative": False, "mint": t["mint"], "owner": t["owner"], "state": "initialized",
                "tokenAmount": ui_amount(t["amount"], self.mints[t["mint"]]),
            }
            out.setdefault(t["owner"], []).append({
                "pubkey": address,
                "account": {"lamports": ATA_RENT, "owner": TOKEN, "executable": False, "rentEpoch": 0, "space": 165,
                            "data": {"program": "spl-token", "space": 165, "parsed": {"info": info, "type": "account"}}},
            })
        return out

    def rpc_document(self, extra_meta: dict | None = None) -> dict:
        meta = {"scenario": self.scenario, "description": self.description, "synthetic": True, "cast": self.cast}
        meta.update(extra_meta or {})
        accounts = {m: self.account_info_for_mint(m) for m in sorted(self.mints, key=lambda a: self.first_seen[a])}
        histories = {}
        for address in sorted(self.history, key=lambda a: self.first_seen[a]):
            entries = sorted(self.history[address], key=lambda e: (e[0], e[1]), reverse=True)
            histories[address] = [
                {"signature": sig, "slot": slot, "err": self.err_of[sig], "memo": None,
                 "blockTime": BASE_TIME + slot, "confirmationStatus": "finalized"}
                for slot, _, sig in entries
            ]
        return {
            "_meta": meta,
            "getAccountInfo": accounts,
            "getSignaturesForAddress": histories,
            "getTransaction": {sig: self.txs[sig] for sig in self.tx_order},
            "getTokenAccountsByOwner": self.token_accounts_by_owner(),
        }


class Tx:
    """Одна транзакція: інструкції застосовуються до стану світу одразу, `finish()` фіксує запис."""

    def __init__(self, world: World, label: str, slot: int, fee_payer: str, *, err=None) -> None:
        self.w, self.label, self.slot, self.err = world, label, slot, err
        self.signers = [fee_payer]
        self.fee_payer = fee_payer
        self.touched: list[str] = []
        self.used_programs: list[str] = []
        self.refs_mints: list[str] = []
        self.top: list[dict] = []
        self.inner: dict[int, list[dict]] = {}
        self.pre_lamports = dict(world.lamports)
        self.pre_tokens = {a: dict(t) for a, t in world.tokens.items()}
        self._touch(fee_payer)

    # --- допоміжне ---
    def _touch(self, *addresses: str) -> None:
        for a in addresses:
            if a not in self.touched:
                self.touched.append(a)
            self.w._see(a)

    def _use(self, program: str) -> None:
        if program not in self.used_programs:
            self.used_programs.append(program)

    def _ref_mint(self, mint: str) -> None:
        if mint not in self.refs_mints:
            self.refs_mints.append(mint)
        self._touch(mint)

    def _credit(self, address: str, amount: int) -> None:
        self.w.lamports[address] = self.w.lamports.get(address, 0) + amount
        assert self.w.lamports[address] >= 0, f"{self.label}: від'ємний баланс {address}"

    # --- інструкції (повертають dict; ефекти застосовуються одразу, якщо транзакція успішна) ---
    def sys_transfer(self, source: str, destination: str, lamports: int) -> dict:
        self._touch(source, destination)
        self._use(SYSTEM)
        if self.err is None:
            self._credit(source, -lamports)
            self._credit(destination, lamports)
        return parsed_ix("system", SYSTEM, "transfer", {"source": source, "destination": destination, "lamports": lamports})

    def sys_create_account(self, source: str, new_account: str, lamports: int, space: int, owner: str) -> dict:
        self._touch(source, new_account)
        self._use(SYSTEM)
        if self.err is None:
            self._credit(source, -lamports)
            self._credit(new_account, lamports)
        return parsed_ix("system", SYSTEM, "createAccount", {
            "lamports": lamports, "newAccount": new_account, "owner": owner, "source": source, "space": space})

    def move_lamports(self, source: str, destination: str, lamports: int) -> None:
        """Зміна балансу програмою напряму (без інструкції System) — як виплата пулом."""
        self._touch(source, destination)
        self._credit(source, -lamports)
        self._credit(destination, lamports)

    def ata_create(self, payer: str, owner: str, mint: str, *, idempotent: bool = False) -> dict:
        address = ata_address(owner, mint)
        self._touch(payer, address, owner)
        self._ref_mint(mint)
        for program in (SYSTEM, TOKEN, ATA_PROGRAM):
            self._use(program)
        ix = parsed_ix("spl-associated-token-account", ATA_PROGRAM, "createIdempotent" if idempotent else "create", {
            "account": address, "mint": mint, "source": payer, "systemProgram": SYSTEM,
            "tokenProgram": TOKEN, "wallet": owner})
        if address not in self.w.tokens:
            create = self.sys_create_account(payer, address, ATA_RENT, 165, TOKEN)
            init = parsed_ix("spl-token", TOKEN, "initializeAccount3", {"account": address, "mint": mint, "owner": owner})
            self.w.tokens[address] = {"owner": owner, "mint": mint, "amount": 0}
            ix["_inner"] = [create, init]
        else:
            assert idempotent, f"{self.label}: рахунок {address} уже існує"
        return ix

    def token_transfer(self, source: str, destination: str, authority: str, amount: int, *,
                       mint: str, checked: bool = True) -> dict:
        self._touch(source, destination, authority)
        self._use(TOKEN)
        if self.err is None:
            for acct, delta in ((source, -amount), (destination, amount)):
                self.w.tokens[acct]["amount"] += delta
                assert self.w.tokens[acct]["amount"] >= 0, f"{self.label}: від'ємний токен-баланс"
        if checked:
            self._ref_mint(mint)
            info = {"authority": authority, "destination": destination, "mint": mint, "source": source,
                    "tokenAmount": ui_amount(amount, self.w.mints[mint])}
            return parsed_ix("spl-token", TOKEN, "transferChecked", info)
        return parsed_ix("spl-token", TOKEN, "transfer", {
            "amount": str(amount), "authority": authority, "destination": destination, "source": source})

    def create_mint(self, payer: str, mint: str) -> list[dict]:
        self._ref_mint(mint)
        self._use(TOKEN)
        create = self.sys_create_account(payer, mint, MINT_RENT, 82, TOKEN)
        init = parsed_ix("spl-token", TOKEN, "initializeMint2", {
            "decimals": self.w.mints[mint], "mint": mint, "mintAuthority": payer})
        return [create, init]

    def mint_to(self, account: str, amount: int, authority: str, *, mint: str) -> dict:
        self._touch(account, authority)
        self._ref_mint(mint)
        self._use(TOKEN)
        self.w.tokens[account]["amount"] += amount
        return parsed_ix("spl-token", TOKEN, "mintTo", {
            "account": account, "amount": str(amount), "mint": mint, "mintAuthority": authority})

    def program_call(self, program: str, accounts: list[str], inner: list[dict] | None = None) -> dict:
        self._touch(*accounts)
        self._use(program)
        data = str(Pubkey(_digest(self.w.scenario, "data", self.label, program)))[:12]
        ix = {"programId": program, "accounts": list(accounts), "data": data}
        if inner:
            ix["_inner"] = inner
        return ix

    def add(self, ix: dict) -> None:
        inner = ix.pop("_inner", None)
        if inner:
            self.inner[len(self.top)] = inner
        self.top.append(ix)

    # --- фіксація ---
    def finish(self) -> str:
        w = self.w
        w.lamports[self.fee_payer] -= FEE
        assert w.lamports[self.fee_payer] >= 0, f"{self.label}: платник не покриває комісію"
        writable = [a for a in self.touched if a not in self.signers and a not in w.programs and a not in self.refs_mints]
        readonly = [m for m in self.refs_mints if m not in self.signers] + [
            p for p in self.used_programs if p not in self.signers]
        keys = self.signers + writable + readonly
        assert len(set(keys)) == len(keys)
        index = {a: i for i, a in enumerate(keys)}

        def balances(snapshot: dict[str, int]) -> list[int]:
            return [snapshot.get(a, 0) for a in keys]

        pre, post = balances(self.pre_lamports), balances(w.lamports)
        assert sum(pre) - FEE == sum(post), f"{self.label}: лампорти не зберігаються"

        def token_side(state: dict[str, dict]) -> list[dict]:
            return [
                {"accountIndex": index[a], "mint": state[a]["mint"], "owner": state[a]["owner"], "programId": TOKEN,
                 "uiTokenAmount": ui_amount(state[a]["amount"], w.mints[state[a]["mint"]])}
                for a in keys if a in state
            ]

        sig = w.signature(self.label)
        assert sig not in w.txs, f"дубль підпису {self.label}"
        raw = {
            "slot": self.slot,
            "blockTime": BASE_TIME + self.slot,
            "version": 0,
            "transaction": {
                "signatures": [sig],
                "message": {
                    "recentBlockhash": str(Pubkey(_digest(w.scenario, "blockhash", self.label))),
                    "accountKeys": [
                        {"pubkey": a, "signer": a in self.signers,
                         "writable": a not in readonly, "source": "transaction"} for a in keys],
                    "instructions": self.top,
                },
            },
            "meta": {
                "err": self.err, "fee": FEE, "preBalances": pre, "postBalances": post,
                "preTokenBalances": token_side(self.pre_tokens), "postTokenBalances": token_side(w.tokens),
                "innerInstructions": [{"index": i, "instructions": ins} for i, ins in sorted(self.inner.items())],
                "logMessages": [], "loadedAddresses": {"readonly": [], "writable": []},
            },
        }
        w.txs[sig] = raw
        w.tx_order.append(sig)
        w.labels[self.label] = (sig, self.slot)
        w.slot_of[sig] = self.slot
        w.err_of[sig] = self.err
        for a in keys:
            if a not in w.programs and a not in w.untracked:
                w.history[a].append((self.slot, len(w.tx_order), sig))
        return sig


# ---------------------------------------------------------------------------------------
# Типові дії сценаріїв
# ---------------------------------------------------------------------------------------


class Launch:
    """Токен із пулом-PDA: створення, наповнення пулу, купівлі, продаж, ейрдроп."""

    def __init__(self, w: World, *, mint: str, dex: str, creator: str, pool: str, vault: str) -> None:
        self.w, self.mint = w, mint
        self.dex, self.creator, self.pool, self.vault = dex, creator, pool, vault
        self.creator_ata = ata_address(creator, mint)
        self.pool_ata = ata_address(pool, mint)

    def schedule_creation(self, slot: int, supply: int, liquidity: int) -> None:
        """Створення mint: усе пропонування карбується одразу — частина в пул, решта творцю (без переказів)."""
        self.w.at(slot, self._create, slot, supply, liquidity)

    def _create(self, slot: int, supply: int, liquidity: int) -> None:
        tx = Tx(self.w, "create_mint", slot, self.creator)
        for ix in tx.create_mint(self.creator, self.mint):
            tx.add(ix)
        tx.add(tx.ata_create(self.creator, self.creator, self.mint))
        tx.add(tx.ata_create(self.creator, self.pool, self.mint))
        tx.add(tx.mint_to(self.creator_ata, supply - liquidity, self.creator, mint=self.mint))
        tx.add(tx.mint_to(self.pool_ata, liquidity, self.creator, mint=self.mint))
        tx.finish()

    def schedule_buy(self, label: str, slot: int, buyer: str, lamports: int, tokens: int) -> None:
        self.w.at(slot, self._buy, label, slot, buyer, lamports, tokens)

    def _buy(self, label: str, slot: int, buyer: str, lamports: int, tokens: int) -> None:
        ata = ata_address(buyer, self.mint)
        tx = Tx(self.w, label, slot, buyer)
        tx.add(tx.ata_create(buyer, buyer, self.mint))
        tx.add(tx.sys_transfer(buyer, self.vault, lamports))
        inner = [tx.token_transfer(self.pool_ata, ata, self.pool, tokens, mint=self.mint)]
        tx.add(tx.program_call(self.dex, [buyer, ata, self.vault, self.pool, self.pool_ata, self.mint], inner))
        tx.finish()

    def schedule_airdrop(self, slot: int, recipient: str, amount: int) -> None:
        self.w.at(slot, self._airdrop, slot, recipient, amount)

    def _airdrop(self, slot: int, recipient: str, amount: int) -> None:
        tx = Tx(self.w, f"airdrop_{recipient[:6]}", slot, self.creator)
        tx.add(tx.ata_create(self.creator, recipient, self.mint))
        tx.add(tx.token_transfer(self.creator_ata, ata_address(recipient, self.mint), self.creator, amount,
                                 mint=self.mint, checked=False))
        tx.finish()

    def schedule_sell(self, label: str, slot: int, seller: str, tokens: int, lamports: int) -> None:
        self.w.at(slot, self._sell, label, slot, seller, tokens, lamports)

    def _sell(self, label: str, slot: int, seller: str, tokens: int, lamports: int) -> None:
        seller_ata = ata_address(seller, self.mint)
        tx = Tx(self.w, label, slot, seller)
        tx.move_lamports(self.pool, seller, lamports)
        inner = [tx.token_transfer(seller_ata, self.pool_ata, seller, tokens, mint=self.mint)]
        tx.add(tx.program_call(self.dex, [seller, seller_ata, self.pool, self.pool_ata, self.mint], inner))
        tx.finish()


def schedule_edge(w: World, label: str, slot: int, sender: str, receiver: str, lamports: int,
                  shape: str, router: str) -> None:
    w.at(slot, _edge, w, label, slot, sender, receiver, lamports, shape, router)


def _edge(w, label, slot, sender, receiver, lamports, shape, router) -> None:
    if shape == "failed":
        tx = Tx(w, label, slot, sender, err=INSTRUCTION_ERROR)
        tx.add(tx.sys_transfer(sender, receiver, lamports))
    else:
        tx = Tx(w, label, slot, sender)
        if shape == "cpi":
            tx.add(tx.program_call(router, [sender, receiver], [tx.sys_transfer(sender, receiver, lamports)]))
        elif shape == "double":
            tx.add(tx.sys_transfer(sender, receiver, lamports))
            tx.add(tx.sys_transfer(sender, receiver, lamports))
        else:
            assert shape == "plain", shape
            tx.add(tx.sys_transfer(sender, receiver, lamports))
    tx.finish()


# ---------------------------------------------------------------------------------------
# Допоміжне для еталона
# ---------------------------------------------------------------------------------------


def transfer_record(w: World, label: str, path: str, sender: str, receiver: str, asset: str,
                    amount: int, depth: int, decimals: int | None) -> dict:
    sig, slot = w.labels[label]
    return {
        "signature": sig, "slot": slot, "block_time": BASE_TIME + slot, "instruction_path": path,
        "sender": w.cast[sender], "receiver": w.cast[receiver], "asset": asset, "amount": amount,
        "decimals": decimals, "depth": depth,
    }


def buyer_records(w: World, buys: list[dict]) -> list[dict]:
    """Покупці за ключем (слот, підпис, гаманець); `buys` — декларація: wallet/label/spent/received/programs."""
    rows = []
    for b in buys:
        sig, slot = w.labels[b["label"]]
        address = w.cast[b["wallet"]]
        rows.append({
            "wallet": address, "rank": 0, "first_buy_signature": sig, "first_buy_slot": slot,
            "first_buy_time": BASE_TIME + slot, "received_amount": b["received"],
            "spent": [{"asset": "sol", "amount": b["spent_sol"]}], "programs": b["programs"],
            "address_type": "wallet" if Pubkey.from_string(address).is_on_curve() else "off_curve",
        })
    rows.sort(key=lambda r: (r["first_buy_slot"], r["first_buy_signature"], r["wallet"]))
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


def sort_transfers(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda t: (t["slot"], t["signature"], t["instruction_path"]))


def excluded_record(w: World, label: str, reason: str, note: str) -> dict:
    sig, slot = w.labels[label]
    return {"signature": sig, "slot": slot, "reason": reason, "note": note}


# ---------------------------------------------------------------------------------------
# Сценарій basic
# ---------------------------------------------------------------------------------------

# (мітка, слот, відправник, отримувач, лампорти, форма, результат)
# результат: ("include", depth) або ("exclude", причина, пояснення)
BASIC_EDGES = [
    ("K_C", 90, "K", "C", 1 * SOL, "plain",
     ("exclude", "beyond_funding_depth", "K фінансує C, C — третій стрибок: переказ у C мав би глибину 4")),
    ("C_B", 100, "C", "B", 5 * SOL, "plain", ("include", 3)),
    ("A_D", 104, "A", "D", 1 * SOL, "plain", ("include", 3)),
    ("D_A", 108, "D", "A", 3 * SOL // 2, "plain", ("include", 2)),
    ("B_A", 110, "B", "A", 4 * SOL, "plain", ("include", 2)),
    ("E_B", 115, "E", "B", 1 * SOL, "plain",
     ("exclude", "causal_cutoff", "E->B після B->A (слот 110): не міг фінансувати ребро B->A, хоч і раніше за купівлю")),
    ("A_P1", 120, "A", "P1", 3 * SOL, "cpi", ("include", 1)),
    ("G_A", 122, "G", "A", 1 * SOL, "plain", ("include", 2)),
    ("A_P2", 125, "A", "P2", 1 * SOL, "double", ("include", 1)),
    ("F_A", 128, "F", "A", 1 * SOL, "plain",
     ("exclude", "causal_cutoff", "F->A після останнього ребра A (A->P2, слот 125): раніше за купівлю, але пізніше за ребро")),
    ("Z_P1", 130, "Z", "P1", 5 * SOL, "failed",
     ("exclude", "failed_transaction", "транзакція з err (недостатньо коштів): переказу не було")),
    ("P3_P3", 150, "P3", "P3", SOL // 10, "plain", ("exclude", "self_transfer", "відправник дорівнює отримувачу")),
    ("X_P1", 205, "X", "P1", 1 * SOL, "plain",
     ("exclude", "after_first_buy", "після першої купівлі P1 (слот 200)")),
]
# двоїстий переказ: у транзакції дві однакові інструкції -> два Transfer (шляхи "0" і "1")
BASIC_SPL = [  # (мітка, слот, сума, шлях, ще є в історії гаманця P2)
    ("S_P2_a", 126, 25_000_000, "0"),
    ("S_P2_b", 127, 5_000_000, "1"),
]
BASIC_BUYS = [
    {"label": "buy_P1", "wallet": "P1", "slot": 200, "spent_sol": 600_000_000, "received": 4_000_000_000},
    {"label": "buy_P2", "wallet": "P2", "slot": 210, "spent_sol": 400_000_000, "received": 2_500_000_000},
    {"label": "buy_P3", "wallet": "P3", "slot": 210, "spent_sol": 300_000_000, "received": 1_500_000_000},
    {"label": "buy_P4", "wallet": "P4", "slot": 220, "spent_sol": 200_000_000, "received": 1_000_000_000},
]
SELL = {"label": "sell_U", "wallet": "P5", "slot": 230, "spent_sol": 2_500_000_000, "received": 20_000_000_000}
BASIC_CONFIG = {
    "first_buyers_n": 5, "funding_depth": 3, "counterparty_threshold": 200,
    "max_signatures_per_wallet": 300, "collect_spl_inbound": True,
}


def build_basic() -> tuple[dict, dict]:
    w = World("basic", "Синтетичний токен: 5 покупців, глибина фінансування 1-3, пастки відсікання (R-1), цикл, "
                       "самопереказ, SPL на існуючий токен-рахунок, PDA-покупець, невдала транзакція.")
    dex, router = w.program("DEX"), w.program("ROUTER")
    mint = w.mint("M", 6)
    creator = w.wallet("CREATOR", lamports=10 * SOL)
    pool = w.pda("P5", [b"pool", bytes(Pubkey.from_string(mint))], dex, lamports=50 * SOL)
    vault = w.wallet("SOL_VAULT", lamports=1 * SOL)
    launch = Launch(w, mint=mint, dex=dex, creator=creator, pool=pool, vault=vault)
    usdc = w.mint("USDC", 6, genesis=True)
    for label, lamports in [("A", 30), ("B", 10), ("C", 20), ("D", 1), ("E", 5), ("F", 5), ("G", 5), ("K", 5),
                            ("X", 5), ("Z", 1), ("S", 1), ("U", 1)]:
        w.wallet(label, lamports=lamports * SOL)
    for label, lamports in [("P1", 10_000_000), ("P2", 10_000_000), ("P3", 1 * SOL), ("P4", 1 * SOL)]:
        w.wallet(label, lamports=lamports)
    c = w.cast
    s_usdc = w.genesis_token_account(c["S"], usdc, 100_000_000)
    p2_usdc = w.genesis_token_account(c["P2"], usdc, 0)

    launch.schedule_creation(10, 1_000_000_000_000, 900_000_000_000)
    launch.schedule_airdrop(12, c["U"], 50_000_000_000)
    for label, slot, sender, receiver, lamports, shape, _ in BASIC_EDGES:
        schedule_edge(w, label, slot, c[sender], c[receiver], lamports, shape, router)
    for label, slot, amount, _ in BASIC_SPL:
        w.at(slot, _basic_spl, w, label, slot, c["S"], c["P2"], s_usdc, p2_usdc, usdc, amount)
    for b in BASIC_BUYS:
        launch.schedule_buy(b["label"], b["slot"], c[b["wallet"]], b["spent_sol"], b["received"])
    launch.schedule_sell(SELL["label"], SELL["slot"], c["U"], SELL["received"], SELL["spent_sol"])
    w.run()

    buy_programs = [ATA_PROGRAM, SYSTEM, dex]
    buys = [dict(b, programs=buy_programs) for b in BASIC_BUYS] + [dict(SELL, programs=[dex])]
    transfers, excluded, notes = [], [], {}
    for label, slot, sender, receiver, lamports, shape, outcome in BASIC_EDGES:
        if outcome[0] == "include":
            paths = ["0.0"] if shape == "cpi" else ["0", "1"] if shape == "double" else ["0"]
            for path in paths:
                transfers.append(transfer_record(w, label, path, sender, receiver, "sol", lamports, outcome[1], None))
                notes[f"{w.sig_of(label)}#{path}"] = f"{sender}->{receiver} depth {outcome[1]} ({shape})"
        else:
            excluded.append(excluded_record(w, label, outcome[1], outcome[2]))
    for label, slot, amount, path in BASIC_SPL:
        transfers.append(transfer_record(w, label, path, "S", "P2", f"spl:{usdc}", amount, 1, 6))
        notes[f"{w.sig_of(label)}#{path}"] = "S->P2 SPL depth 1"
    transfers = sort_transfers(transfers)
    for b in buys:
        excluded.append(excluded_record(
            w, b["label"], "in_first_buy_transaction",
            f"доставка токена в транзакції першої купівлі {b['wallet']} — це сама купівля, а не фінансування; "
            "межа строга (research R-1: строго раніше)"))

    expected = {
        "scenario": "basic",
        "description": w.description,
        "mint": launch.mint,
        "wallets": w.cast,
        "config": BASIC_CONFIG,
        "rules": {
            "funding_depth": "для funding_depth=d беруться перекази з depth <= d; еталон складено для d=3",
            "first_buyers_n": "для N<5 беруться перші N з buyers (порядок (slot, signature, wallet)); "
                              "перекази відповідних покупців — за depth/шляхом від них",
            "collect_spl_inbound": "при false усі перекази з asset 'spl:*' відсутні",
            "same_slot_tie": "P2 і P3 купують в одному слоті: порядок за підписом (research R-6), "
                             "тому P3 (rank 2) стоїть перед P2 (rank 3)",
            "cutoff_is_strict": "межа — транзакція (перша купівля або ребро): береться лише те, що в історії "
                                "строго раніше; доставка mint у самій купівельній транзакції не входить",
        },
        "completeness": {"status": "complete", "missing": [], "buyers_complete": True},
        "buyers": buyer_records(w, buys),
        "transfers": transfers,
        "unexpanded": [],
        "excluded": excluded,
        "transfer_notes": notes,
    }
    return w.rpc_document(), expected


def _basic_spl(w, label, slot, sender, receiver, sender_ata, receiver_ata, mint, amount) -> None:
    tx = Tx(w, label, slot, sender)
    if label.endswith("_b"):  # відправник «гарантує» ATA отримувача: гаманець P2 потрапляє в accountKeys
        tx.add(tx.ata_create(sender, receiver, mint, idempotent=True))
    tx.add(tx.token_transfer(sender_ata, receiver_ata, sender, amount, mint=mint))
    tx.finish()


# ---------------------------------------------------------------------------------------
# Сценарій hub
# ---------------------------------------------------------------------------------------

HUB_HISTORY = 1200  # усього підписів H
HUB_OLD = HUB_HISTORY - 3  # до ребра H->Q1: решта — саме ребро й два пост-ребрових вхідні
HUB_INBOUND_J = [17 + 61 * i for i in range(20)]  # позиції (0 = найновіший до ребра) вхідних від S1..S5
HUB_OLDEST_J = HUB_OLD - 1  # S6 — найстаріший запис
HUB_EDGE_SLOT = 3001
HUB_BASE_SLOT = 3000


def hub_slot(j: int) -> int:
    return HUB_BASE_SLOT - 2 * j


def build_hub() -> tuple[dict, dict]:
    w = World("hub", "Хаб H: 1200 підписів і 6 унікальних відправників; фінансує покупця Q1. "
                     "Три випадки конфігурації: поріг зв'язності, ліміт підписів, контроль без обмежень.")
    dex = w.program("DEX")
    mint = w.mint("M", 6)
    creator = w.wallet("CREATOR", lamports=10 * SOL)
    pool = w.pda("POOL", [b"pool", bytes(Pubkey.from_string(mint))], dex, lamports=50 * SOL)
    vault = w.wallet("SOL_VAULT", lamports=1 * SOL)
    launch = Launch(w, mint=mint, dex=dex, creator=creator, pool=pool, vault=vault)
    c = w.cast
    hub = w.wallet("H", lamports=100 * SOL)
    senders = [w.wallet(f"S{k}", lamports=10 * SOL) for k in range(1, 7)]
    w.wallet("T1", lamports=5 * SOL)
    w.wallet("U1", lamports=5 * SOL)
    w.wallet("W", lamports=5 * SOL)
    w.wallet("Q1", lamports=50_000_000)
    w.wallet("Q2", lamports=50_000_000)
    recipients = [w.wallet(f"R{k}", tracked=False) for k in range(1, 8)]
    router = w.program("ROUTER")

    launch.schedule_creation(10, 1_000_000_000_000, 900_000_000_000)
    inbound = {}  # j -> (мітка, відправник)
    for i, j in enumerate(HUB_INBOUND_J):
        inbound[j] = (f"in_{i}", f"S{(i % 5) + 1}")
    inbound[HUB_OLDEST_J] = ("in_S6", "S6")
    for j in range(HUB_OLD):
        if j in inbound:
            label, sender = inbound[j]
            schedule_edge(w, label, hub_slot(j), c[sender], hub, 1 * SOL, "plain", router)
        else:
            schedule_edge(w, f"out_{j}", hub_slot(j), hub, recipients[j % 7], 1_000_000, "plain", router)
    schedule_edge(w, "T1_S1", 301, c["T1"], c["S1"], 1 * SOL, "plain", router)
    schedule_edge(w, "U1_S1", 2981, c["U1"], c["S1"], 1 * SOL, "plain", router)
    schedule_edge(w, "H_Q1", HUB_EDGE_SLOT, hub, c["Q1"], 10 * SOL, "plain", router)
    schedule_edge(w, "post_0", 3011, c["S1"], hub, 1 * SOL, "plain", router)
    schedule_edge(w, "post_1", 3021, c["S1"], hub, 1 * SOL, "plain", router)
    schedule_edge(w, "W_Q2", 3050, c["W"], c["Q2"], 2 * SOL, "plain", router)
    launch.schedule_buy("buy_Q1", 3100, c["Q1"], 6 * SOL, 4_000_000_000)
    launch.schedule_buy("buy_Q2", 3110, c["Q2"], 1 * SOL, 2_000_000_000)
    w.run()
    assert len(w.history[hub]) == HUB_HISTORY

    buys = [
        {"label": "buy_Q1", "wallet": "Q1", "spent_sol": 6 * SOL, "received": 4_000_000_000,
         "programs": [ATA_PROGRAM, SYSTEM, dex]},
        {"label": "buy_Q2", "wallet": "Q2", "spent_sol": 1 * SOL, "received": 2_000_000_000,
         "programs": [ATA_PROGRAM, SYSTEM, dex]},
    ]

    def case(name, config, window, *, include_oldest, expand_senders, unexpanded):
        transfers = [
            transfer_record(w, "H_Q1", "0", "H", "Q1", "sol", 10 * SOL, 1, None),
            transfer_record(w, "W_Q2", "0", "W", "Q2", "sol", 2 * SOL, 1, None),
        ]
        excluded = [
            excluded_record(w, "post_0", "causal_cutoff", "S1->H після ребра H->Q1 (слот 3001)"),
            excluded_record(w, "post_1", "causal_cutoff", "S1->H після ребра H->Q1 (слот 3001)"),
        ]
        for j, (label, sender) in sorted(inbound.items()):
            if j == HUB_OLDEST_J and not include_oldest:
                reason = "over_counterparty_threshold" if config["counterparty_threshold"] < 6 else "beyond_signature_cap"
                excluded.append(excluded_record(w, label, reason, "шостий унікальний відправник / за межею вікна підписів"))
            elif j >= window:
                excluded.append(excluded_record(w, label, "beyond_signature_cap", f"позиція {j} глибша за вікно {window}"))
            else:
                transfers.append(transfer_record(w, label, "0", sender, "H", "sol", 1 * SOL, 2, None))
        if expand_senders:
            transfers.append(transfer_record(w, "T1_S1", "0", "T1", "S1", "sol", 1 * SOL, 3, None))
            excluded.append(excluded_record(w, "U1_S1", "causal_cutoff",
                                            "U1->S1 після останнього ребра S1 у зібраних переказах хаба"))
        else:
            excluded.append(excluded_record(w, "T1_S1", "sender_not_expanded_high_degree",
                                            "S1 — відправник хаба; через хаб з високою зв'язністю не розгортається"))
            excluded.append(excluded_record(w, "U1_S1", "sender_not_expanded_high_degree",
                                            "те саме: S1 не розгортається"))
        return {
            "name": name, "config": config,
            "completeness": {"status": "complete", "missing": [], "buyers_complete": True},
            "unexpanded": unexpanded, "transfers": sort_transfers(transfers), "excluded": excluded,
        }

    base = {"first_buyers_n": 2, "funding_depth": 3, "collect_spl_inbound": True}
    cases = [
        case("hub_high_degree", dict(base, counterparty_threshold=5, max_signatures_per_wallet=2000), HUB_OLD,
             include_oldest=False, expand_senders=False,
             unexpanded=[{"wallet": hub, "depth": 1, "reason": "high_degree", "counterparties_seen": 6,
                          "signatures_truncated": False, "not_asserted": ["signatures_seen"]}]),
        case("hub_signature_cap", dict(base, counterparty_threshold=10, max_signatures_per_wallet=300), 300,
             include_oldest=False, expand_senders=True,
             unexpanded=[{"wallet": hub, "depth": 1, "reason": "signature_cap", "counterparties_seen": 5,
                          "signatures_seen": 300, "signatures_truncated": True}]),
        case("hub_control", dict(base, counterparty_threshold=10, max_signatures_per_wallet=2000), HUB_OLD,
             include_oldest=True, expand_senders=True, unexpanded=[]),
    ]
    expected = {
        "scenario": "hub",
        "description": w.description,
        "mint": launch.mint,
        "wallets": w.cast,
        "assumptions": [
            "Поріг перевищено, коли унікальних відправників стало БІЛЬШЕ порога; переказ відправника, що "
            "перевищив поріг, не збирається, counterparties_seen = поріг + 1 (data-model: 'понад поріг не збираються').",
            "signatures_seen при high_degree залежить від розміру сторінки/пакета і в еталоні не фіксується (not_asserted).",
            "Ліміт підписів: переглядаються рівно max_signatures_per_wallet найновіших записів до межі H.",
        ],
        "buyers": buyer_records(w, buys),
        "cases": cases,
    }
    return w.rpc_document(), expected


# ---------------------------------------------------------------------------------------
# Сценарій corrupt
# ---------------------------------------------------------------------------------------


def build_corrupt() -> dict:
    w = World("corrupt", "Покупці K1-K3: фінансування K1 здорове, getTransaction для переказу в K2 повертає null, "
                         "транзакція переказу в K3 прийшла без meta.")
    dex, router = w.program("DEX"), w.program("ROUTER")
    mint = w.mint("M", 6)
    creator = w.wallet("CREATOR", lamports=10 * SOL)
    pool = w.pda("POOL", [b"pool", bytes(Pubkey.from_string(mint))], dex, lamports=50 * SOL)
    vault = w.wallet("SOL_VAULT", lamports=1 * SOL)
    launch = Launch(w, mint=mint, dex=dex, creator=creator, pool=pool, vault=vault)
    c = w.cast
    for label in ("F1", "F2", "F3"):
        w.wallet(label, lamports=5 * SOL)
    for label in ("K1", "K2", "K3"):
        w.wallet(label, lamports=50_000_000)
    launch.schedule_creation(10, 1_000_000_000_000, 900_000_000_000)
    for k in (1, 2, 3):
        schedule_edge(w, f"F{k}_K{k}", 99 + k, c[f"F{k}"], c[f"K{k}"], 2 * SOL, "plain", router)
        launch.schedule_buy(f"buy_K{k}", 190 + 10 * k, c[f"K{k}"], 1 * SOL, 1_000_000_000 * k)
    w.run()
    doc = w.rpc_document()
    null_sig, broken_sig = w.sig_of("F2_K2"), w.sig_of("F3_K3")
    doc["getTransaction"][null_sig] = None
    del doc["getTransaction"][broken_sig]["meta"]
    doc["_meta"]["defects"] = {"null_transaction": [null_sig], "missing_meta": [broken_sig]}
    return doc


# ---------------------------------------------------------------------------------------
# Сценарій notfound
# ---------------------------------------------------------------------------------------


def build_notfound() -> dict:
    w = World("notfound", "Токена немає: рахунок відсутній; адреса — звичайний гаманець System Program; "
                          "адреса — токен-рахунок (належить Token, але не mint).")
    missing = w.wallet("MISSING_MINT")
    system_owned = w.wallet("SYSTEM_OWNED")
    owner = w.wallet("TOKEN_ACCOUNT_OWNER")
    mint = w.mint("SOME_MINT", 6, genesis=True)
    token_account = w.genesis_token_account(owner, mint, 1_000_000)
    w.cast["TOKEN_ACCOUNT"] = token_account
    info = w.token_accounts_by_owner()[owner][0]["account"]
    return {
        "_meta": {"scenario": w.scenario, "description": w.description, "synthetic": True, "cast": w.cast},
        "getAccountInfo": {
            missing: None,
            system_owned: {"lamports": 2_500_000_000, "owner": SYSTEM, "executable": False, "rentEpoch": 0,
                           "space": 0, "data": ["", "base64"]},
            token_account: info,
        },
        "getSignaturesForAddress": {},
        "getTransaction": {},
        "getTokenAccountsByOwner": {},
    }


# ---------------------------------------------------------------------------------------
# Серіалізація й запис
# ---------------------------------------------------------------------------------------


def _compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def dump_pretty(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def dump_lines(data: dict) -> str:
    """Корінь і методи розгорнуті, кожен запис (транзакція/підпис) — один рядок: діфи читаються."""
    out = ["{"]
    top = list(data.items())
    for ti, (key, value) in enumerate(top):
        comma = "," if ti < len(top) - 1 else ""
        if key.startswith("_") or not isinstance(value, dict) or not value:
            out.append(f" {json.dumps(key)}: {_compact(value)}{comma}")
            continue
        out.append(f" {json.dumps(key)}: {{")
        items = list(value.items())
        for ii, (k, v) in enumerate(items):
            icomma = "," if ii < len(items) - 1 else ""
            if isinstance(v, list) and v:
                out.append(f"  {json.dumps(k)}: [")
                for ei, element in enumerate(v):
                    out.append(f"   {_compact(element)}{',' if ei < len(v) - 1 else ''}")
                out.append(f"  ]{icomma}")
            else:
                out.append(f"  {json.dumps(k)}: {_compact(v)}{icomma}")
        out.append(f" }}{comma}")
    out.append("}")
    text = "\n".join(out) + "\n"
    assert json.loads(text) == data
    return text


def build_all() -> dict[str, str]:
    """Повертає {відносний шлях: текст} для всіх згенерованих файлів."""
    basic_rpc, basic_expected = build_basic()
    hub_rpc, hub_expected = build_hub()
    return {
        "basic/rpc.json": dump_pretty(basic_rpc),
        "basic/expected.json": dump_pretty(basic_expected),
        "hub/rpc.json": dump_lines(hub_rpc),
        "hub/expected.json": dump_pretty(hub_expected),
        "corrupt/rpc.json": dump_pretty(build_corrupt()),
        "notfound/rpc.json": dump_pretty(build_notfound()),
    }


def main(argv: list[str]) -> int:
    built = build_all()
    stale = []
    for rel, text in built.items():
        path = SCENARIOS_DIR / rel
        if "--check" in argv:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(SCENARIOS_DIR.parent.parent)} ({len(text)} bytes)")
    if stale:
        print("stale:", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
