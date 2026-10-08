# verifies: FR-008-05
"""Адреса клієнта за зворотним проксі (T-165, R-4): довіра до X-Forwarded-For лише від довіреного peer.

Чиста функція `client_address`; сокетів і мережі немає. Межові випадки — IPv6, IPv4-mapped,
сміття, порт у записі, scope id, довгий заголовок, порожній `trusted`.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

from unmask.delivery import http as http_mod
from unmask.delivery.config import load_protection_config
from unmask.delivery.http import client_address

ROOT = Path(__file__).resolve().parents[1]
LOOP = [ipaddress.ip_network("127.0.0.1/32")]
NETS = [ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("127.0.0.1/32")]


# --- вимоги з рядка T-165 ---------------------------------------------------------------

def test_untrusted_peer_ignores_header() -> None:
    # клієнт напряму підробляє заголовок — ключ лишається його справжньою адресою
    assert client_address("203.0.113.9", "1.2.3.4", LOOP) == "203.0.113.9"
    assert client_address("203.0.113.9", "6.6.6.6, 1.2.3.4", NETS) == "203.0.113.9"


def test_trusted_peer_takes_rightmost_untrusted() -> None:
    assert client_address("127.0.0.1", "1.2.3.4", LOOP) == "1.2.3.4"
    assert client_address("127.0.0.1", "5.5.5.5, 1.2.3.4", LOOP) == "1.2.3.4"


def test_spoofed_left_entries_are_ignored() -> None:
    assert client_address("127.0.0.1", "6.6.6.6, 1.2.3.4", LOOP) == "1.2.3.4"
    # підробка зліва не обходить ліміт: ключ — крайній правий, дописаний проксі
    assert client_address("127.0.0.1", "6.6.6.6, 7.7.7.7, 8.8.8.8, 1.2.3.4", LOOP) == "1.2.3.4"


def test_chain_of_trusted_proxies_skipped() -> None:
    assert client_address("10.0.0.1", "1.2.3.4, 10.0.0.2, 10.9.9.9", NETS) == "1.2.3.4"
    # недовірений запис посеред ланцюга зупиняє розбір: клієнт — він, а не лівіші
    assert client_address("10.0.0.1", "1.2.3.4, 10.0.0.2, 5.5.5.5, 10.9.9.9", NETS) == "5.5.5.5"


def test_whole_chain_trusted_gives_peer() -> None:
    assert client_address("10.0.0.1", "10.0.0.2, 10.0.0.3", NETS) == "10.0.0.1"


def test_garbage_entry_falls_back_to_peer() -> None:
    for header in ("garbage", "unknown", "1.2.3.4, garbage", "1.2.3.4:5678", "[::1]",
                   "999.1.1.1", "1.2.3", "1.2.3.4/32", "01.2.3.4", "'1.2.3.4'", "1.2.3.4;x"):
        assert client_address("127.0.0.1", header, LOOP) == "127.0.0.1", header


def test_garbage_to_the_left_of_untrusted_entry_is_never_reached() -> None:
    # правий запис уже недовірений валідний — розбір зупинено до сміття зліва
    assert client_address("127.0.0.1", "garbage, 1.2.3.4", LOOP) == "1.2.3.4"


def test_garbage_after_trusted_hop_stops_parsing() -> None:
    assert client_address("10.0.0.1", "1.2.3.4, garbage, 10.0.0.2", NETS) == "10.0.0.1"


def test_missing_or_empty_header_gives_peer() -> None:
    for header in (None, "", "   ", ",", " , ,", "\t"):
        assert client_address("127.0.0.1", header, LOOP) == "127.0.0.1", repr(header)


def test_ipv6_peer_and_entries() -> None:
    nets = [ipaddress.ip_network("::1/128"), ipaddress.ip_network("fd00::/8")]
    assert client_address("::1", "2001:db8::5", nets) == "2001:db8::5"
    assert client_address("fd00::7", "2001:db8::5, fd00::8", nets) == "2001:db8::5"
    # недовірений IPv6-peer не може підмінити адресу
    assert client_address("2001:db8::9", "2001:db8::5", nets) == "2001:db8::9"
    # нормалізація: верхній регістр і розгорнута форма → str(ip_address)
    assert client_address("::1", "2001:0DB8:0000:0000:0000:0000:0000:0005", nets) == "2001:db8::5"
    # змішаний ланцюг: IPv4-клієнт за IPv6-проксі
    assert client_address("::1", "1.2.3.4", nets) == "1.2.3.4"


def test_empty_trusted_list_never_trusts() -> None:
    for empty in ([], (), iter(())):
        assert client_address("127.0.0.1", "1.2.3.4", empty) == "127.0.0.1"


# --- межові: нормалізація, пробіли, регістр, порожні елементи ------------------------------

def test_whitespace_and_empty_elements_tolerated() -> None:
    assert client_address("127.0.0.1", "  1.2.3.4  ", LOOP) == "1.2.3.4"
    assert client_address("127.0.0.1", "6.6.6.6,,\t1.2.3.4 ,", LOOP) == "1.2.3.4"
    assert client_address("10.0.0.1", "1.2.3.4 ,\t10.0.0.2,  , ", NETS) == "1.2.3.4"


def test_multiple_headers_joined_by_comma_are_one_chain() -> None:
    # викликач зливає кілька X-Forwarded-For через кому в порядку надходження
    assert client_address("10.0.0.1", "6.6.6.6" + "," + "1.2.3.4, 10.0.0.2", NETS) == "1.2.3.4"


def test_ipv4_mapped_entry_normalized_to_ipv4() -> None:
    # один клієнт — один ключ, незалежно від запису
    assert client_address("127.0.0.1", "::ffff:1.2.3.4", LOOP) == "1.2.3.4"
    assert client_address("127.0.0.1", "::FFFF:0102:0304", LOOP) == "1.2.3.4"
    assert client_address("127.0.0.1", "1.2.3.4", LOOP) == client_address(
        "127.0.0.1", "::ffff:1.2.3.4", LOOP)


def test_ipv4_mapped_peer_normalized_and_matches_ipv4_trusted() -> None:
    # dual-stack сокет повідомляє 127.0.0.1 як ::ffff:127.0.0.1 — це той самий довірений проксі
    assert client_address("::ffff:127.0.0.1", "1.2.3.4", LOOP) == "1.2.3.4"
    # недовірений mapped-peer нормалізується до IPv4 (не два кошики для одного клієнта)
    assert client_address("::ffff:203.0.113.9", "1.2.3.4", LOOP) == "203.0.113.9"
    assert client_address("::FFFF:203.0.113.9", None, LOOP) == "203.0.113.9"


def test_ipv4_mapped_entry_matches_ipv4_trusted_network() -> None:
    # mapped-запис довіреного проксі пропускається як довірена ланка
    assert client_address("10.0.0.1", "1.2.3.4, ::ffff:10.0.0.2", NETS) == "1.2.3.4"


def test_ipv4_mapped_network_in_trusted_matches_ipv4_peer() -> None:
    trusted = [ipaddress.ip_network("::ffff:10.0.0.0/104")]
    assert client_address("10.1.2.3", "1.2.3.4", trusted) == "1.2.3.4"
    assert client_address("11.1.2.3", "1.2.3.4", trusted) == "11.1.2.3"


def test_scope_id_in_entry_is_garbage_not_a_new_bucket() -> None:
    # інакше "fe80::1%a", "fe80::1%b" … дали б необмежену кількість ключів
    assert client_address("127.0.0.1", "fe80::1%eth0", LOOP) == "127.0.0.1"
    assert client_address("127.0.0.1", "1.2.3.4, fe80::1%eth0", LOOP) == "127.0.0.1"


def test_scope_id_in_peer_is_dropped() -> None:
    assert client_address("fe80::1%eth0", None, LOOP) == "fe80::1"
    assert client_address("fe80::1%eth1", None, LOOP) == client_address("fe80::1%eth0", None, LOOP)


def test_peer_not_a_valid_ip_returned_as_is_without_raising() -> None:
    for peer in ("", "unix-socket", "not an ip", "localhost", "1.2.3.4:80"):
        assert client_address(peer, "1.2.3.4", LOOP) == peer
        assert client_address(peer, None, LOOP) == peer


def test_valid_peer_is_normalized_even_without_header() -> None:
    assert client_address("2001:0DB8::0005", None, []) == "2001:db8::5"
    assert client_address("203.0.113.9", None, []) == "203.0.113.9"


def test_trusted_accepts_addresses_networks_and_strings() -> None:
    mixed = [ipaddress.ip_address("127.0.0.1"), ipaddress.ip_network("10.0.0.0/8"),
             "192.168.0.0/16", "::1", ipaddress.ip_address("fd00::1")]
    for peer in ("127.0.0.1", "10.2.3.4", "192.168.5.5", "::1", "fd00::1"):
        assert client_address(peer, "1.2.3.4", mixed) == "1.2.3.4", peer
    assert client_address("192.169.0.1", "1.2.3.4", mixed) == "192.169.0.1"
    assert client_address("fd00::2", "1.2.3.4", mixed) == "fd00::2"


def test_invalid_trusted_entry_is_ignored_fail_safe() -> None:
    # конфіг уже відхиляє такі записи; тут — захист у глибину: менше довіри, не виняток
    assert client_address("127.0.0.1", "1.2.3.4", ["not-an-ip", "300.1.1.1"]) == "127.0.0.1"
    assert client_address("127.0.0.1", "1.2.3.4", ["not-an-ip", "127.0.0.1"]) == "1.2.3.4"


def test_trusted_is_consumed_once_so_iterators_work() -> None:
    assert client_address("127.0.0.1", "1.2.3.4", iter(LOOP)) == "1.2.3.4"


def test_catch_all_trusted_network_skips_everything_to_peer() -> None:
    # 0.0.0.0/0 довіряє будь-якому peer; усе ланцюжок довірений → peer
    assert client_address("203.0.113.9", "1.2.3.4", ["0.0.0.0/0"]) == "203.0.113.9"


def test_returns_str_always() -> None:
    assert type(client_address("127.0.0.1", "1.2.3.4", LOOP)) is str
    assert type(client_address("::1", None, [])) is str


# --- захист від DoS: обмежена довжина розбору ----------------------------------------------

def test_header_length_limit_is_documented_constant() -> None:
    assert isinstance(http_mod._XFF_MAX_LENGTH, int) and 256 <= http_mod._XFF_MAX_LENGTH <= 65536
    assert "_XFF_MAX_LENGTH" in (http_mod.client_address.__doc__ or "") or \
        "довжин" in (http_mod.client_address.__doc__ or "")


def test_oversized_header_keeps_rightmost_entries() -> None:
    limit = http_mod._XFF_MAX_LENGTH
    junk = ", ".join(["6.6.6.6"] * (limit // 3))  # значно довше за ліміт
    header = junk + ", 1.2.3.4"
    assert len(header) > limit
    # хвіст зберігається: клієнт, дописаний проксі справа, визначається правильно
    assert client_address("127.0.0.1", header, LOOP) == "1.2.3.4"


def test_oversized_header_cut_mid_entry_never_produces_a_partial_address() -> None:
    limit = http_mod._XFF_MAX_LENGTH
    # перша обрізана ланка була б "…4.5" — частковий запис не повинен стати клієнтом
    header = "9" * (limit + 50) + ", 10.0.0.2"
    assert client_address("10.0.0.1", header, NETS) == "10.0.0.1"
    header = "1.2.3.4" * (limit // 7 + 5)  # один гігантський запис
    assert client_address("127.0.0.1", header, LOOP) == "127.0.0.1"


def test_oversized_header_fragment_of_cut_entry_is_not_taken_as_client() -> None:
    limit = http_mod._XFF_MAX_LENGTH
    # "97.7.7.7" обрізається рівно на перший символ: залишок "7.7.7.7" — валідна IP, але не клієнт
    first = "97.7.7.7"
    n, k = divmod(limit + 1 - len(first), len(", 10.0.0.2"))
    header = first + " " * k + ", 10.0.0.2" * n
    assert len(header) == limit + 1
    assert client_address("10.0.0.1", header, NETS) == "10.0.0.1"


def test_oversized_all_trusted_tail_falls_back_to_peer_not_to_hidden_left_client() -> None:
    limit = http_mod._XFF_MAX_LENGTH
    header = "1.2.3.4, " + ", ".join(["10.0.0.2"] * (limit // 5))
    assert len(header) > limit
    # лівий клієнт відсічений обрізкою — консервативно peer, не підміна й не виняток
    assert client_address("10.0.0.1", header, NETS) == "10.0.0.1"


def test_header_exactly_at_limit_is_fully_parsed() -> None:
    limit = http_mod._XFF_MAX_LENGTH
    tail = ", 10.0.0.2"
    head = "1.2.3.4"
    pad = limit - len(head) - len(tail)
    header = head + " " * pad + tail
    assert len(header) == limit
    assert client_address("10.0.0.1", header, NETS) == "1.2.3.4"


def test_many_trusted_entries_do_not_blow_up() -> None:
    header = ", ".join(["10.0.0.2"] * 2000)
    assert client_address("10.0.0.1", header, NETS) == "10.0.0.1"


# --- інтеграція з типом конфігу ------------------------------------------------------------

def test_works_with_real_config_trusted_proxies() -> None:
    cfg = load_protection_config(ROOT / "config" / "protection.yaml")
    # реальний тип ProtectionConfig.trusted_proxies — tuple[str, ...]; функція приймає його як є
    assert all(isinstance(x, str) for x in cfg.trusted_proxies)
    peer = str(ipaddress.ip_network(cfg.trusted_proxies[0], strict=False).network_address) \
        if cfg.trusted_proxies else None
    if peer is not None:
        assert client_address(peer, "1.2.3.4", cfg.trusted_proxies) == "1.2.3.4"
    assert client_address("203.0.113.77", "1.2.3.4", cfg.trusted_proxies) == "203.0.113.77"
