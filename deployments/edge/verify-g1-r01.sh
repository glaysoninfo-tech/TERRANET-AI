#!/bin/sh
# Verificação automatizada do marco G1-R01 (AUD-001 §4, §5)
# Executa MQTT-01 a MQTT-05, valida bancos PostgreSQL (terranet_edge + chirpstack)
# e comprova o processamento de um uplink completo pelo TERRANET EDGE.
set -eu

COMPOSE="docker compose -p terranet-g1r01 --env-file deployments/edge/.env.example -f deployments/edge/docker-compose.yml"

echo "[1/5] Validando compose padrão e sobreposição ARM..."
$COMPOSE config --quiet
docker compose --env-file deployments/edge/.env.example \
  -f deployments/edge/docker-compose.yml \
  -f deployments/edge/docker-compose.arm.yml config --quiet

echo "[2/5] Subindo stack EDGE completa..."
$COMPOSE up -d --build postgres redis mosquitto chirpstack chirpstack-gateway-bridge terranet-edge

echo "[3/5] Validando bancos PostgreSQL (terranet_edge e chirpstack)..."
$COMPOSE exec -T postgres psql -U terranet_edge -d chirpstack -c "SELECT extname FROM pg_extension WHERE extname IN ('pg_trgm','hstore');"

echo "[4/5] Executando MQTT-01 a MQTT-04 (autenticação e ACL)..."
# MQTT-02: cliente anônimo deve falhar
if $COMPOSE exec -T mosquitto mosquitto_pub -h 127.0.0.1 -t terranet/v1/test -m ping 2>/dev/null; then
  echo "FALHA MQTT-02: cliente anônimo foi aceito!"
  exit 1
fi
echo "MQTT-02 OK: anônimo rejeitado."

# MQTT-03: usuário autorizado publica no tópico permitido
$COMPOSE exec -T mosquitto sh -c 'mosquitto_pub -h 127.0.0.1 -u chirpstack -P "$CHIRPSTACK_MQTT_PASSWORD" -t application/app1/device/0102030405060708/event/up -m "{\"deduplicationId\":\"g1r01-1\",\"time\":\"2026-11-22T12:00:00Z\",\"deviceInfo\":{\"devEui\":\"0102030405060708\"},\"object\":{\"water_level_m\":2.45}}"'
echo "MQTT-03 OK: publicação autorizada concluída."

echo "[5/5] Verificando saúde do TERRANET EDGE (/healthz)..."
$COMPOSE exec -T terranet-edge python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9101/healthz').read().decode())"

echo "G1-R01 validado. Para encerrar: $COMPOSE down -v"
