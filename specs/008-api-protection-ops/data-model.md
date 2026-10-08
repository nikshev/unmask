# Data Model: Захист публічного API і операційна готовність (008)

Стан — лише в пам'яті процесу (assumption spec): перезапуск скидає лічильники. Жоден тип
не впливає на кластери, оцінку чи смугу; успішний документ 004.1 проходить без змін.

Розташування: `src/unmask/delivery/limits.py` (нове), `delivery/config.py` (+завантажувач),
`delivery/http.py`, `delivery/service.py`, `delivery/bot.py`, `delivery/main.py` (зміни).

## `ProtectionConfig` (`delivery/config.py`, frozen dataclass)

Джерело — `config/protection.yaml` (R-5, R-7). Тихих умовчань немає: відсутнє/невідоме
поле, `bool` замість `int`, значення < 1, нерозбірна адреса проксі → `ConfigError(<поле>)`.

| Поле | Ключ YAML | Тип / межі |
|---|---|---|
| `version` | `version` | int ≥ 1 |
| `api_requests_per_window` | `api.requests_per_window` | int ≥ 1 |
| `api_window_seconds` | `api.window_seconds` | int ≥ 1 |
| `max_concurrent_analyses` | `heavy.max_concurrent` | int ≥ 1 |
| `busy_retry_after_seconds` | `heavy.busy_retry_after_seconds` | int ≥ 1 |
| `trusted_proxies` | `proxy.trusted` | tuple[str], кожен — `ipaddress.ip_network(x, strict=False)`; може бути порожнім |
| `bot_checks_per_window` | `bot.checks_per_window` | int ≥ 1 |
| `bot_window_seconds` | `bot.window_seconds` | int ≥ 1 |
| `digest` | — | sha256 канонічного вмісту (`hubs.config.content_digest`) |

`load_protection_config(path) -> ProtectionConfig`. Журнал не читає (перевірка — тест,
як для `delivery.yaml`).

## `Decision` (frozen)

`allowed: bool`, `retry_after_seconds: int` (0, якщо `allowed`; інакше ≥ 1).

## `SlidingWindowLimiter`

`SlidingWindowLimiter(limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic)`

- `hit(key) -> Decision` — атомарно (один `threading.Lock`): відкинути мітки `≤ now − window`;
  мітки `> now` (регрес годинника) затиснути до `now`; якщо міток `< limit` — записати `now`,
  `allowed=True`; інакше НЕ записувати, `retry_after = clamp(ceil(oldest + window − now), 1, ceil(window))`.
- Прибирання: не частіше разу на `window` секунд — видалити ключі без живих міток (пам'ять
  не росте від одноразових адрес).
- Інваріанти: у будь-якому ключі ≤ `limit` міток; `retry_after ≤ ceil(window)` за будь-якої
  поведінки годинника; після `advance(retry_after)` наступний `hit` того ж ключа — `allowed`.

## `HeavySlots` і `Lease`

`HeavySlots(capacity: int, busy_retry_after_seconds: int)`

- `try_acquire() -> Lease | None` — без очікування; `None`, якщо `in_use == capacity`.
- `in_use: int` (для тестів і SC-003), `retry_after_seconds: int`.
- `Lease` — контекст-менеджер; `release()` ідемпотентний (подвійне звільнення не зменшує
  лічильник нижче реального); `__exit__` звільняє за будь-якого винятку, включно з `BaseException`.
- Інваріант: `0 ≤ in_use ≤ capacity` завжди; після завершення всіх власників `in_use == 0`.

## `UserGate` (бот)

`UserGate(checks_per_window: int, window_seconds: float, clock=time.monotonic)`

- `try_enter(user_key, *, counts: bool) -> GateDecision` — атомарно:
  1. користувач уже активний → `GateDecision("in_progress", 0)`;
  2. `counts` і вікно користувача вичерпане → `GateDecision("rate_limited", retry_after)`
     (те саме правило, що `SlidingWindowLimiter`, всередині);
  3. інакше позначити активним → `GateDecision("ok", 0)`.
- `leave(user_key)` — ідемпотентно знімає позначку (викликається у `finally` воркера).
- `refund(user_key) -> bool` — повертає квоту (знімає останню позначку вікна цього користувача), якщо його
  поточний допуск її списав (`counts=True`) і ще не повернув; викликається воркером, коли відповідь —
  глобальний `busy` (відмова системи не на рахунку користувача); не більше одного разу на допуск; ні-оп
  після `leave`. Всередині — `SlidingWindowLimiter.forget_last(key)`.
- `counts=False` для `/check` на кешований токен або невалідну адресу (не «важкий» аналіз):
  вікно не споживається, але правило «один одночасний» діє.

`GateDecision`: `kind ∈ {"ok", "in_progress", "rate_limited"}`, `retry_after_seconds: int`.

## `Protection` (один регулятор на процес)

`Protection(api: SlidingWindowLimiter, slots: HeavySlots, users: UserGate, trusted_proxies: tuple[IPv4Network | IPv6Network, ...])`

`build_protection(cfg: ProtectionConfig, clock=time.monotonic) -> Protection`. Створюється
один раз у `main.py`; `slots` іде в `DeliveryService` (отже спільний для HTTP і бота — обидва
викликають той самий сервіс), `api` + `trusted_proxies` — у `http.serve`, `users` — у
`run_polling`.

## Документ перевантаження (контракт `contracts/overload-error.schema.json`)

```json
{"mint": "<сегмент шляху як отримано>",
 "error": {"kind": "rate_limited" | "busy", "detail": "<людський текст>", "retry_after_seconds": 17}}
```

HTTP: `rate_limited` → `429`, `busy` → `503`, заголовок `Retry-After: <retry_after_seconds>`.
Документ ніколи не несе полів результату (`clusters`, `risk_score`, `band`, …) і не кешується.

## Зміни наявних інтерфейсів (сумісні: без нових аргументів поведінка 004 побайтово та сама)

| Де | Було | Стає |
|---|---|---|
| `DeliveryService.__init__` | `(ingest, graph, clusters, cache=None)` | `+ slots: HeavySlots \| None = None` |
| `DeliveryService` | — | `+ needs_analysis(mint) -> bool`: `True` ⇔ формат адреси валідний і кешу немає |
| `http` | `handle_token_request(mint, service)` | без змін; `_ERROR_STATUS += {rate_limited: 429, busy: 503}` |
| `http` | — | `+ client_address(peer, forwarded_for, trusted) -> str` |
| `http` | — | `+ handle_get(path, peer, forwarded_for, service, protection=None) -> (status, doc, headers)` |
| `http` | `serve(service, port)` | `serve(service, port, *, host="0.0.0.0", protection=None)` |
| `bot` | `run_polling(transport, service, *, render, evidence_limit, stop_after, sleep)` | `+ gate: UserGate \| None = None, spawn: Callable[[Callable[[], None]], None] \| None = None` |
| `bot._format_error` | 3 гілки | `+ busy`, `+ rate_limited` тексти з `contracts/overload.md` |
| `main` | `--port`, `--no-bot`, `--config-dir` | `+ --host`; читає `protection.yaml`; відсутній/некоректний → exit 2 до відкриття порту |

## Потік

```
HTTP GET /api/token/{mint}
  route ─ health ─▶ 200 (без лімітів)
        ─ token ─▶ client_address(peer, XFF, trusted) ─▶ api.hit(client)
                     ├─ denied ─▶ 429 + Retry-After (сервіс не викликається)
                     └─ allowed ─▶ handle_token_request ─▶ DeliveryService.analyze
                                      ├─ кеш-хіт ───────────────▶ 200 (слот не потрібен)
                                      ├─ невалідний формат ─────▶ 400 (без слота)
                                      └─ single_flight(mint, _guarded_build)
                                           ├─ приєднання ───────▶ результат власника (без слота)
                                           └─ власник: slots.try_acquire()
                                                ├─ None ─▶ _ErrorDoc(busy) ─▶ 503 + Retry-After (не кешується)
                                                └─ with lease: _build ─▶ 200/400 ; finally release

Bot /check <mint> (з gate)
  потік опитування: users.try_enter(user, counts=service.needs_analysis(mint))
     ├─ in_progress / rate_limited ─▶ текст відмови, аналіз не запускається
     └─ ok ─▶ spawn(worker): handle_check(...) ; finally users.leave(user)
               └─ service.analyze ─▶ busy-документ ─▶ «service is busy … ~N s» (без progress)
```
