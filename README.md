# TERRANET AI — PIT-001 (Projeto de Integração Tecnológica)

Implementação completa da arquitetura **TERRANET EDGE** + **TERRANET CORE** especificada no **PIT-001 v1.0** para o MVP (**H01** nível de rio LoRaWAN, **P01** bomba supervisionada Modbus e **V01** rastreador veicular GNSS/LTE).

## Estrutura do repositório

```text
.
├── common/                 # Banco portátil (PostgreSQL/SQLite), DeviceStore, tempo UTC, métricas Prometheus
├── contracts/              # Modelo canônico v1.0, tópicos MQTT, comandos assinados, dispositivos, regras e JSON Schemas (E02)
├── edge/                   # TERRANET EDGE (autonomia local / offline-first)
│   ├── adapters/           # LoRaWAN/ChirpStack (E04), Modbus + CLP de bancada (E06), TRACK GNSS/LTE (E05)
│   ├── commands/           # Command Validator (assinatura, TTL, estado esperado, permissivos)
│   ├── monitoring/         # Monitoring Agent e métricas Prometheus (§18, E11)
│   ├── mqtt/               # MessageBus com políticas de QoS e retenção (§10)
│   ├── normalization/      # Conversão de unidades, validação de faixas e qualidade (§9)
│   ├── registry/           # Device Registry local (E03)
│   ├── rules/              # Motor de regras determinístico com histerese e versionamento (E07)
│   ├── storage/            # Persistência local + Transactional Outbox (E08)
│   └── synchronization/    # Sync Agent assíncrono e idempotente (E08)
├── core/                   # TERRANET CORE modular em Python / FastAPI (E09)
│   ├── api/                # API Gateway (/ingest/batches, /ingest/track, /devices, /commands, /geospatial, /analytics, /audit)
│   ├── analytics/          # Analytics nível inicial (§22.1)
│   ├── commands/           # Emissão, assinatura por EDGE e máquina de estados (§14)
│   ├── devices/            # Gestão de dispositivos e ciclo de vida (§8)
│   ├── geospatial/         # Consultas PostGIS / Haversine (ADR-008)
│   ├── identity/           # Autenticação, RBAC e isolamento multicliente (§19.3, §21)
│   ├── storage/            # Esquemas PostgreSQL+PostGIS e SQLite
│   └── telemetry/          # Ingestão idempotente de lotes EDGE e pacotes TRACK
├── demo/                   # Implementação de referência pit_demo.py e Dockerfile (§16, §17.2)
├── deployments/            # Dockerfiles e Docker Compose para EDGE (AU915 ChirpStack) e Cloud (E10)
├── docs/                   # Arquitetura (E01), critérios de aceitação (E12) e segurança operacional
└── tests/                  # Suíte integrada PIT-T01 a PIT-T15
```

## Execução rápida

### 1. Instalar em modo desenvolvimento

```bash
pip install -e ".[edge,core,dev]"
```

### 2. Executar a demonstração de referência (§16)

```bash
python demo/pit_demo.py
```

### 3. Executar todos os testes (unitários, integração e aceitação PIT-T01..T15)

```bash
pytest -v
```

### 4. Executar a IHC Web local (modo bancada interativa)

```bash
python -m core.demo_server
# Abra http://127.0.0.1:8000 no navegador (OpenAPI em /docs e Prometheus em /metrics)
```

### 5. Executar o TERRANET CORE isoladamente

```bash
# Bootstrap inicial de cliente, instalação, usuário operador e EDGE
python -m core.cli create-tenant CLIENTE-001 "Fazenda Modelo"
python -m core.cli create-site FAZENDA-001 CLIENTE-001 "Sede"
python -m core.cli create-user operador CLIENTE-001 --role operator
python -m core.cli register-edge EDGE-001 CLIENTE-001 FAZENDA-001

# Iniciar API (FastAPI / Uvicorn em http://127.0.0.1:8000/docs)
python -m core.main
```

### 6. Contêineres Docker (E10)

```bash
# Demo isolada (§17.2)
docker build -t terranet-pit-demo ./demo
docker run --rm --network none --read-only terranet-pit-demo

# Stack EDGE (ChirpStack AU915 + Mosquitto + PostgreSQL + TERRANET EDGE + Prometheus)
cp deployments/edge/.env.example deployments/edge/.env
docker compose -f deployments/edge/docker-compose.yml up -d

# Stack CORE (PostGIS + TERRANET CORE + Prometheus + Grafana)
cp deployments/cloud/.env.example deployments/cloud/.env
docker compose -f deployments/cloud/docker-compose.yml up -d
```

## Documentação

* [Arquitetura de software (E01)](docs/architecture.md)
* [Protocolos e tópicos MQTT](contracts/protocols/README.md)
* [Critérios de aceitação PIT-T01..T15 (E12)](docs/acceptance-tests.md)
* [Segurança operacional e recuperação](docs/operations-security.md)
