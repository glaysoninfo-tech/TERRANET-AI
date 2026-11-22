#!/bin/sh
set -eu

PASSWD_FILE="/mosquitto/data/passwd"
: "${EDGE_MQTT_PASSWORD:?defina EDGE_MQTT_PASSWORD}"
: "${CHIRPSTACK_MQTT_PASSWORD:?defina CHIRPSTACK_MQTT_PASSWORD}"
: "${GW_BRIDGE_MQTT_PASSWORD:?defina GW_BRIDGE_MQTT_PASSWORD}"

mkdir -p /mosquitto/data
rm -f "$PASSWD_FILE"
touch "$PASSWD_FILE"
chmod 0600 "$PASSWD_FILE"

mosquitto_passwd -b "$PASSWD_FILE" terranet_edge "$EDGE_MQTT_PASSWORD"
mosquitto_passwd -b "$PASSWD_FILE" chirpstack "$CHIRPSTACK_MQTT_PASSWORD"
mosquitto_passwd -b "$PASSWD_FILE" gw_bridge "$GW_BRIDGE_MQTT_PASSWORD"

chown mosquitto:mosquitto "$PASSWD_FILE" 2>/dev/null || true
chmod 0700 "$PASSWD_FILE"

exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf
