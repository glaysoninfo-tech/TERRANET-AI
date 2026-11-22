# Decisões Técnicas — POC-001 Gate 1

Este documento registra as decisões de engenharia aprovadas para o fechamento do **Gate 1 (Software + Bancada Controlada)** da **POC-001** antes da aquisição completa de campo e antes da arquitetura de fazenda completa (PIT-002).

## Decisões aprovadas

1. **Fonte única de contratos:** os esquemas do PIT-001 (`contracts/schemas/*.json`) prevalecem sobre os exemplos ilustrativos da POC-001 §12 (`location.lat/lon`, `operation`, assinatura HMAC por EDGE e escopo multicliente).
2. **Duplo caminho no P01 (automação hidráulica):**
   * **P01-A (local):** `EDGE → Modbus TCP ou RTU (RS-485) → CLP` ([edge/adapters/modbus/client.py](../edge/adapters/modbus/client.py)).
   * **P01-B (remoto):** `EDGE → ChirpStack Downlink (Classe C) → NODE-C → Modbus RTU → CLP` com confirmação assíncrona via uplink `fPort 20` ([edge/adapters/lorawan/downlink.py](../edge/adapters/lorawan/downlink.py)).
3. **Estratégia híbrida para TRACK (V01 — TRACK-B e TRACK-I CAN/J1939):**
   * **Gateway TCP nativo Teltonika Codec 8 / 8E** em Python puro ([core/track_gateway/](../core/track_gateway/)) com verificação CRC-16/IBM, ACK binário pós-persistência (para esvaziamento seguro do buffer offline — `POC-T12`) e leitura passiva de CAN/J1939 (RPM, temperatura do motor, combustível, horímetro).
   * **Compatibilidade Traccar Webhook** (`POST /api/v1/ingest/track/webhook` + perfil opcional `traccar` no compose do CORE) para integrar rastreadores de outros fabricantes sem reescrever parsers proprietários.
4. **Hardware EDGE de referência:** computador **ARM industrial de baixo consumo (≤ 5 W)** com **RTC com bateria** e NTP local, perfil dedicado em [deployments/edge/docker-compose.arm.yml](../deployments/edge/docker-compose.arm.yml), para manter o conjunto Gateway + EDGE + LTE dentro da meta de autonomia ≥ 48 h (100 Ah / 300 Wp).
5. **Hardening mínimo de campo:** Mosquitto com autenticação e ACL ([deployments/edge/mosquitto/acl.conf](../deployments/edge/mosquitto/acl.conf)), proxy HTTPS/TLS Caddy ([deployments/cloud/Caddyfile](../deployments/cloud/Caddyfile)), políticas RLS PostgreSQL ([postgresql_rls.sql](../core/storage/migrations/postgresql_rls.sql)), regra hidrológica em 4 faixas (`NORMAL / ATENÇÃO / ALERTA / CRÍTICO`), notificações (Telegram/Webhook) e guarda de sanidade de relógio RTC no EDGE.
6. **Relatório automático `POC-E16`:** módulo [core/poc_report.py](../core/poc_report.py) e endpoint `GET /api/v1/poc/report` calculando PDR real por Node, registros atrasados de rastreadores, segurança de comandos, taxa de reutilização `R` e classificação **GO / CONDITIONAL GO / NO-GO**.
