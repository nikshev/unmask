# Contract: розширення результату фічі 001 для swap-and-send (зворотно сумісне)

Єдина зміна контракту 001, яку робить фіча 002 (FR-002-21, SC-006; research R-3). Файл `specs/001-onchain-data-ingest/contracts/ingest-result.schema.json` редагується задачею T-044 рівно так, як описано тут; `spec.md` фічі 001 не змінюється.

## Зміни схеми → версія 1.1

1. `"$id": "https://unmask.local/schemas/001/ingest-result-1.1.json"`, `"title": "unmask ingest outcome (feature 001, schema 1.1)"`.
2. У `$defs/result.properties` додати `"delegated": { "$ref": "#/$defs/delegated" }`. **Не** додавати до `required`; `additionalProperties: false` лишити — старі документи (без ключа) валідні, нові — теж.
3. Додати визначення:

```json
"delegatedSide": { "enum": ["payer", "receiver"] },

"delegatedLink": {
  "type": "object",
  "additionalProperties": false,
  "required": ["signature", "slot", "block_time", "payer", "receiver"],
  "properties": {
    "signature": { "$ref": "#/$defs/signature" },
    "slot": { "$ref": "#/$defs/nonNegInt" },
    "block_time": { "oneOf": [ { "type": "null" }, { "$ref": "#/$defs/nonNegInt" } ] },
    "payer": { "$ref": "#/$defs/address" },
    "receiver": { "$ref": "#/$defs/address" }
  }
},

"unpairedCandidate": {
  "type": "object",
  "additionalProperties": false,
  "required": ["signature", "slot", "block_time", "wallet", "side", "detail"],
  "properties": {
    "signature": { "$ref": "#/$defs/signature" },
    "slot": { "$ref": "#/$defs/nonNegInt" },
    "block_time": { "oneOf": [ { "type": "null" }, { "$ref": "#/$defs/nonNegInt" } ] },
    "wallet": { "$ref": "#/$defs/address" },
    "side": { "$ref": "#/$defs/delegatedSide" },
    "detail": { "type": "string", "pattern": "^payers=[0-9]+ receivers=[0-9]+$" }
  }
},

"delegated": {
  "description": "Swap-and-send analysis over the same mint transactions the purchase rule scanned (feature 002, FR-002-15..19). complete mirrors completeness.buyers.complete; not_analyzed marks a result built without the analysis and is never emitted by the collector.",
  "type": "object",
  "additionalProperties": false,
  "required": ["links", "unpaired", "complete", "reason", "detail"],
  "properties": {
    "links": { "type": "array", "items": { "$ref": "#/$defs/delegatedLink" } },
    "unpaired": { "type": "array", "items": { "$ref": "#/$defs/unpairedCandidate" } },
    "complete": { "type": "boolean" },
    "reason": { "oneOf": [ { "type": "null" }, { "$ref": "#/$defs/missingReason" }, { "const": "not_analyzed" } ] },
    "detail": { "type": "string" }
  },
  "allOf": [
    {
      "if": { "properties": { "complete": { "const": true } } },
      "then": { "properties": { "reason": { "type": "null" } } },
      "else": { "properties": { "reason": { "type": "string" } } }
    }
  ]
}
```

4. У `$defs/result` додати перехресний інваріант (FR-002-19 через похідність від `buyers.complete`):

```json
"allOf": [
  {
    "description": "delegated.complete mirrors completeness.buyers.complete when the analysis ran",
    "if": { "required": ["delegated"], "properties": { "delegated": { "properties": { "reason": { "not": { "const": "not_analyzed" } } } } } },
    "then": {
      "anyOf": [
        { "properties": { "completeness": { "properties": { "buyers": { "properties": { "complete": { "const": true } } } } },
                          "delegated": { "properties": { "complete": { "const": true } } } } },
        { "properties": { "completeness": { "properties": { "buyers": { "properties": { "complete": { "const": false } } } } },
                          "delegated": { "properties": { "complete": { "const": false } } } } }
      ]
    }
  }
]
```

## Серіалізація (`ingest/serialize.py`)

`to_dict(IngestResult)` **завжди** додає ключ `"delegated"`:

```json
"delegated": {
  "links": [ { "signature": "...", "slot": 210, "block_time": 1759400210, "payer": "...", "receiver": "..." } ],
  "unpaired": [ { "signature": "...", "slot": 230, "block_time": 1759400230, "wallet": "...", "side": "payer", "detail": "payers=2 receivers=2" } ],
  "complete": true,
  "reason": null,
  "detail": ""
}
```

Порядок `links` — `(slot, signature, payer, receiver)`, `unpaired` — `(slot, signature, wallet, side)`. Решта ключів `to_dict` і їхні значення не змінюються ні на байт; `to_json` лишається канонічним (`sort_keys`).

## Гарантії для споживачів 001 (перевіряються T-044, T-046)

- `expected.json` усіх наявних сценаріїв — без змін (SC-006).
- `to_dict(result)` без ключа `delegated` == результат до змін (знімок sha256 канонічного JSON для `basic`, `hub`, `corrupt` при фіксованих конфігах, записаний у тесті до внесення змін).
- `buyers[]` (склад, порядок, `rank`), `transfers[]`, `unexpanded[]`, `completeness`, `metadata` — незмінні (FR-002-17).
- Результати колектора й сервісу ніколи не мають `delegated.reason == "not_analyzed"`.
- `resume == fresh` поширюється на `delegated` (research R-4).
