# E12 — Matriz de Critérios de Aceitação (PIT-001 §25 e §33)

Todos os 15 critérios de aceitação da seção 25 e as três aplicações do MVP (`H01`, `P01`, `V01`) estão automatizados na suíte `tests/test_pit_acceptance.py` e nos testes de módulo.

| ID | Critério PIT-001 | Cobertura automatizada |
|---|---|---|
| **PIT-T01** | Cadastro e identificação corretos de dispositivo | `tests/test_pit_acceptance.py::test_pit_t01_to_t08_h01_lorawan_offline_sync_and_idempotence`, `contracts/tests/test_contracts.py` |
| **PIT-T02** | Recepção de uplink LoRaWAN homologado (ChirpStack) | `tests/test_pit_acceptance.py::test_pit_t01_to_t08_h01_lorawan_offline_sync_and_idempotence`, `edge/tests/test_adapters.py` |
| **PIT-T03** | Conversão para modelo canônico (2 fabricantes distintos) | `edge/tests/test_adapters.py::test_two_different_lorawan_manufacturers_produce_same_canonical_model` |
| **PIT-T04** | Duplicidade: nenhum evento lógico duplicado | `tests/test_pit_acceptance.py`, `edge/tests/test_storage_and_sync.py` |
| **PIT-T05** | Persistência local em transação com outbox | `edge/tests/test_storage_and_sync.py`, `tests/test_pit_acceptance.py` |
| **PIT-T06** | Falha de internet: continuidade local e motor de regras | `tests/test_pit_acceptance.py::test_pit_t01_to_t08_h01_lorawan_offline_sync_and_idempotence` |
| **PIT-T07** | Sincronização automática de pendentes após reconexão | `tests/test_pit_acceptance.py`, `edge/tests/test_storage_and_sync.py` |
| **PIT-T08** | Falha após persistência central: retransmissão sem duplicação | `tests/test_pit_acceptance.py`, `edge/tests/test_storage_and_sync.py`, `demo/pit_demo.py` |
| **PIT-T09** | Rastreamento GNSS/LTE (V01) | `tests/test_pit_acceptance.py::test_pit_t09_v01_tracker_gnss`, `core/tests/test_core_api.py` |
| **PIT-T10** | Comando não autorizado rejeitado (RBAC CORE + HMAC EDGE) | `tests/test_pit_acceptance.py::test_pit_t10_to_t12_p01_pump_commands_security_expiration_interlock` |
| **PIT-T11** | Comando expirado rejeitado sem execução física | `tests/test_pit_acceptance.py::test_pit_t10_to_t12_p01_pump_commands_security_expiration_interlock` |
| **PIT-T12** | Falha de intertravamento: ausência de execução física | `edge/tests/test_rules_and_commands.py`, `tests/test_pit_acceptance.py` |
| **PIT-T13** | Reinicialização do EDGE com recuperação consistente | `tests/test_pit_acceptance.py::test_pit_t13_t14_t15_restart_audit_and_tenant_isolation` |
| **PIT-T14** | Auditoria das operações relevantes (CORE e EDGE) | `tests/test_pit_acceptance.py::test_pit_t13_t14_t15_restart_audit_and_tenant_isolation` |
| **PIT-T15** | Isolamento estrito entre clientes | `core/tests/test_core_api.py`, `tests/test_pit_acceptance.py` |

## Limitações conhecidas do MVP

1. **Testes em bancada**: o acionamento P01 utiliza `SimulatedPumpPLC` com intertravamentos em software/simulador; antes de operar equipamentos reais em campo é obrigatória validação física com profissional habilitado (PIT-001 §25, §29, §33).
2. **PostgreSQL / PostGIS e MQTT real**: os testes padrão executam sobre SQLite e `InMemoryBus` para máxima portabilidade sem infraestrutura externa; os esquemas PostgreSQL/PostGIS (`migrations/postgresql.sql`) e o stack `deployments/*/docker-compose.yml` estão prontos para validação com contêineres Docker.
3. **mTLS**: o transporte `HttpSyncTransport` suporta certificados cliente (`client_cert`, `ca_bundle`); a terminação TLS/mTLS em produção deve ser configurada no proxy reverso / ingress do CORE.
