#!/usr/bin/env bash
# impl: FR-008-11, FR-008-12, FR-008-13
#
# Перевірка розгортання оператором (FR-008-11, FR-008-12, FR-008-13). Лише читає з сервера:
# без ключів, без запитів на реальні токени (mint навмисно невалідний — відсікається до RPC).
#
# Використання:
#   deploy/smoke.sh <domain>
#       Обов'язкові перевірки розгортання (усі мають дати ok):
#         systemctl show unmask -p Restart  == always   (без systemctl — збій)
#         http://<domain>/healthz            → 301
#         https://<domain>/healthz           → 200
#         https://<domain>/api/token/<невалідний mint> → 400
#
#   deploy/smoke.sh --local [BASE_URL] [BURST]
#       Локальна перевірка без systemctl, 301 і TLS:
#         BASE_URL за замовчуванням $SMOKE_URL або http://127.0.0.1:18080
#         /healthz → 200; невалідний mint → 400
#         BURST — скільки невалідних mint надіслати для демонстрації 429 (0 — пропустити).
#         Невалідний mint рахується лімітом, але до RPC не доходить.
#
# Код виходу: 0 — усі перевірки ok; 1 — є збій; 2 — помилка аргументів (з usage).
set -euo pipefail

usage() {
    echo "usage: $0 <domain>" >&2
    echo "       $0 --local [BASE_URL] [BURST]" >&2
    exit 2
}

INVALID_MINT="not-a-mint"
FAILS=0
PASSES=0

report() {
    # report <ok|FAIL> <назва> <деталі>
    printf '%-5s %-36s %s\n' "$1" "$2" "$3"
    case "$1" in
        ok) PASSES=$((PASSES + 1)) ;;
        FAIL) FAILS=$((FAILS + 1)) ;;
    esac
}

# http_code <url> → код HTTP або 000 при збої з'єднання; ніколи не падає.
http_code() {
    curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null || true
}

expect_code() {
    # expect_code <назва> <url> <очікуваний код>
    local code
    code="$(http_code "$2")"
    if [ "$code" = "$3" ]; then
        report ok "$1" "HTTP $code"
    else
        report FAIL "$1" "очікувався HTTP $3, отримано $code ($2)"
    fi
}

MODE_LOCAL=0
if [ $# -lt 1 ]; then
    usage
fi

if [ "$1" = "--local" ]; then
    MODE_LOCAL=1
    if [ $# -gt 3 ]; then
        usage
    fi
    BASE="${2:-${SMOKE_URL:-http://127.0.0.1:18080}}"
    BASE="${BASE%/}"
    case "$BASE" in
        http://*|https://*) ;;
        *) usage ;;
    esac
    BURST="${3:-0}"
    case "$BURST" in
        ''|*[!0-9]*) usage ;;
    esac
else
    if [ $# -ne 1 ]; then
        usage
    fi
    DOMAIN="$1"
    if ! [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
        usage
    fi
fi

if [ "$MODE_LOCAL" -eq 1 ]; then
    echo "smoke (local): $BASE"
    expect_code "healthz" "$BASE/healthz" 200
    expect_code "невалідний mint → 400" "$BASE/api/token/$INVALID_MINT" 400

    if [ "$BURST" -gt 0 ]; then
        limited=0
        i=1
        while [ "$i" -le "$BURST" ]; do
            if [ "$(http_code "$BASE/api/token/burst-$i")" = "429" ]; then
                limited=$((limited + 1))
            fi
            i=$((i + 1))
        done
        if [ "$limited" -gt 0 ]; then
            report ok "ліміт запитів → 429" "$limited із $BURST запитів"
        else
            report FAIL "ліміт запитів → 429" "жодного 429 із $BURST запитів"
        fi
    fi
else
    echo "smoke (domain): $DOMAIN"
    if command -v systemctl >/dev/null 2>&1; then
        restart="$(systemctl show unmask -p Restart --value 2>/dev/null || true)"
        if [ "$restart" = "always" ]; then
            report ok "systemd Restart=always" "unmask"
        else
            report FAIL "systemd Restart=always" "unmask: Restart=${restart:-?}"
        fi
    else
        report FAIL "systemd Restart=always" "systemctl недоступний — перевірка неможлива"
    fi
    expect_code "HTTP → 301 на HTTPS" "http://$DOMAIN/healthz" 301
    expect_code "HTTPS /healthz → 200" "https://$DOMAIN/healthz" 200
    expect_code "HTTPS невалідний mint → 400" "https://$DOMAIN/api/token/$INVALID_MINT" 400
fi

echo "smoke: ok=$PASSES fail=$FAILS"
if [ "$FAILS" -gt 0 ]; then
    exit 1
fi
exit 0
