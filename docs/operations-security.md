# Segurança Operacional, Disponibilidade e Atualização (PIT-001 §19, §24, §28.2)

## Segurança em camadas (§19)

1. **Dispositivos**: perfis no Device Registry rejeitam chaves criptográficas (`app_key`, `password`, `token`, etc.) — credenciais pertencem exclusivamente ao gerenciador de segredos e à camada de rede (ChirpStack).
2. **EDGE**:
   * Imagens Docker executam com usuário não privilegiado (`10001:10001`).
   * Portas administrativas (Mosquitto `1883`, ChirpStack `8080`, Prometheus `9091/9101`) expostas apenas em `127.0.0.1` no compose de referência.
   * Comandos físicos exigem assinatura HMAC-SHA256 derivada individualmente por EDGE (`derive_edge_key`), prazo de validade (`5..600 s`), estado `ACTIVE`, versão de configuração compatível e operação autorizada.
   * Regras marcadas como `safety_critical=true` são recusadas em atualizações remotas (§15.3).
3. **CORE**:
   * Autenticação Bearer com hash SHA-256 em banco (nunca em texto plano).
   * RBAC (`viewer < operator < admin`) e isolamento por `tenant_id` em todos os endpoints.
   * Conferência de escopo do EDGE (`tenant_id` + `site_ids`) e propriedade do dispositivo em `/api/v1/ingest/batches`.
   * Limitador de requisições por EDGE (HTTP 429).
4. **Tecnologia Operacional (NIST SP 800-82 Rev. 3)**:
   * Intertravamentos críticos (proteção contra funcionamento a seco, sobrecarga, emergência, seletor local/remoto) permanecem no CLP.
   * Comandos em estado `UNKNOWN` não são retransmitidos automaticamente.

## Política de recuperação (§24)

* **Internet indisponível**: EDGE continua ingerindo eventos, executando regras locais e acumulando no `sync_outbox`.
* **CORE indisponível / falha pós-commit**: backoff exponencial com jitter; retransmissão idempotente sem duplicação.
* **Reinicialização do EDGE**: banco persistente (PostgreSQL ou SQLite WAL com `synchronous=FULL`) preserva dispositivos, regras, estado de histerese e outbox pendente.
* **Controlador Modbus indisponível**: leitura falha controlada sem perder eventos anteriores; comandos retornam `FAILED` (antes da escrita) ou `UNKNOWN` (durante escrita/confirmação).
