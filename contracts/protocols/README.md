# Protocolos TERRANET

## 1. Tópicos MQTT (PIT-001 §10)

```
terranet/v1/{tenant}/{site}/{device}/telemetry       QoS 1, sem retenção
terranet/v1/{tenant}/{site}/{device}/state           QoS 1, retido (estado atual)
terranet/v1/{tenant}/{site}/{device}/event           QoS 1, sem retenção
terranet/v1/{tenant}/{site}/{device}/command         QoS 1, NUNCA retido
terranet/v1/{tenant}/{site}/{device}/command-result  QoS 1, persistido
```

Níveis de tópico são normalizados com `contracts.topics.slug` (minúsculas, `[a-z0-9_-]`).
Os tópicos nativos do ChirpStack (`application/{application_id}/device/{dev_eui}/event/{event}`)
permanecem na camada de entrada; aplicações de negócio consomem apenas tópicos TERRANET.

QoS 1 = entrega *pelo menos uma vez*: todo consumidor deve deduplicar por `event_id`.

## 2. Sincronização EDGE → CORE (PIT-001 §12–13)

`POST /api/v1/ingest/batches` — `Authorization: Bearer <token do EDGE>` (+ mTLS no proxy).

* Corpo: `contracts/schemas/ingest_batch.v1.json`.
* Resposta: `accepted` (persistidos, inclusive duplicados já conhecidos), `rejected`
  (permanentes, com motivo), `retryable` (temporários).
* O EDGE só marca o outbox como `SYNCED` para IDs presentes em `accepted`.
* IDs ausentes da resposta são tratados como `retryable`.
* Garantia: *at-least-once* com deduplicação lógica no destino.

## 3. Comandos CORE → EDGE (PIT-001 §14)

* Operador cria comando (`POST /api/v1/commands`), CORE valida permissão, estado do
  equipamento, operação permitida e EDGE online; assina com chave derivada por EDGE.
* EDGE busca comandos por conexão de saída (`GET /api/v1/edges/{edge_id}/commands`),
  sem portas abertas na instalação remota.
* EDGE valida assinatura, prazo, estado esperado, versão de configuração e autoridade;
  o adaptador industrial verifica permissivos; o CLP mantém os intertravamentos.
* Resultados (`RECEIVED`, `ACCEPTED`, `EXECUTED`, `VERIFIED`, `REJECTED`, `EXPIRED`,
  `FAILED`, `UNKNOWN`) retornam como eventos `command_result` pelo outbox.
* Comandos não confirmados **não** são retransmitidos automaticamente.

## 4. Configuração (PIT-001 §20)

`GET /api/v1/edges/{edge_id}/config` retorna a *intenção* de configuração (dispositivos e
regras). O EDGE valida, aplica o que é autorizado e informa a versão efetivamente aplicada
em `POST /api/v1/edges/{edge_id}/config-reports`.
