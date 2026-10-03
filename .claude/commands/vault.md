---
description: Перебудувати Obsidian-vault з графом репозиторію (доки, спеки, код, тести)
---

Виконай обидва кроки:

```bash
export PATH="$HOME/.local/bin:$PATH"
graphify update .                          # перебудувати граф з коду, без LLM
graphify export obsidian --dir ./vault     # згенерувати нотатки + canvas
```

Якщо `graphify` не встановлено:
`uv tool install graphifyy && graphify install --platform claude`.

Якщо після рефакторингу вузлів стало менше, `update` відмовиться перезаписувати
граф — тоді `graphify update . --force`.

Що індексується, визначає `.graphifyignore`: зміст проекту так, нутрощі
інструментів (`.specify/`, `.claude/skills/`, `.agents/skills/`) ні. Не прибирай
ці виключення — без них vault тоне в шаблонах spec-kit.

Запускай після мержу кожної фічі. Vault — другий, візуальний вьювер тієї самої
матриці, яку `scripts/trace.py` перевіряє машинно.

Після завершення скажи, скільки вузлів і спільнот у графі, і чи зʼявились
несподівані звʼязки між фічами — це часто ознака протікання меж модулів.
