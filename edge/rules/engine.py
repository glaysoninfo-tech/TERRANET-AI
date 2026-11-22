"""Motor de regras local determinístico com versionamento e histerese (PIT-001 §15, E07).

Tipos suportados (§15.2):
* ``threshold``: comparação com histerese (evita oscilação perto do limiar).
* ``trend``: taxa de variação (unidade/hora) dentro de uma janela temporal.
* ``persistence``: condição sustentada por ``duration_s`` segundos.
* ``combination``: todas as condições verdadeiras simultaneamente.
* ``state``: equipamento ligado fora da janela de horas permitidas.
* ``no_data``: ausência de telemetria além de ``max_silence_s``.
* ``battery``: tensão/nível abaixo do mínimo, com histerese.

Segurança (§15.3):
* Regras recebidas remotamente são validadas antes da ativação.
* Regras com ``safety_critical=True`` não podem ser alteradas nem desabilitadas
  remotamente.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from common.db import Database, dumps, loads
from common.device_store import DeviceStore
from common.timeutil import iso, now_iso, parse_ts, utc_now
from contracts.canonical import CanonicalEvent, EventType, Quality, TimestampSource
from contracts.rules import Rule, RuleValidationError, compare
from edge.storage.repository import EdgeStore


class RuleStore:
    def __init__(self, db: Database):
        self.db = db

    def get(self, rule_id: str) -> Rule | None:
        row = self.db.query_one(
            "SELECT definition FROM automation_rules WHERE rule_id = ?", (rule_id,)
        )
        return Rule.from_dict(loads(row["definition"])) if row else None

    def list(self, enabled_only: bool = False) -> list[Rule]:
        sql = "SELECT definition FROM automation_rules"
        if enabled_only:
            sql += " WHERE enabled = 1" if self.db.dialect == "sqlite" else " WHERE enabled = TRUE"
        rows = self.db.query(sql + " ORDER BY rule_id")
        return [Rule.from_dict(loads(r["definition"])) for r in rows]

    def apply(
        self, rule: Rule, actor: str = "local", remote: bool = False
    ) -> tuple[bool, str]:
        """Valida e ativa/atualiza regra; bloqueia alteração remota de safety_critical."""
        now = now_iso()
        try:
            rule.validate()
        except RuleValidationError as exc:
            self._log_history(rule.rule_id, rule.version, None, "apply", actor, "rejected", str(exc), now)
            return False, str(exc)

        existing = self.get(rule.rule_id)
        if remote and ((existing and existing.safety_critical) or rule.safety_critical):
            reason = "regras de segurança física não podem ser alteradas remotamente"
            self._log_history(rule.rule_id, rule.version, rule.to_dict(), "remote_update",
                              actor, "rejected", reason, now)
            return False, reason

        if existing and rule.version < existing.version:
            reason = f"versão regressiva ({rule.version} < {existing.version})"
            self._log_history(rule.rule_id, rule.version, rule.to_dict(), "apply",
                              actor, "rejected", reason, now)
            return False, reason

        enabled_val: Any = int(rule.enabled) if self.db.dialect == "sqlite" else bool(rule.enabled)
        sc_val: Any = (
            int(rule.safety_critical)
            if self.db.dialect == "sqlite"
            else bool(rule.safety_critical)
        )
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO automation_rules (rule_id, version, definition, enabled, "
                "safety_critical, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (rule_id) DO UPDATE SET version = excluded.version, "
                "definition = excluded.definition, enabled = excluded.enabled, "
                "safety_critical = excluded.safety_critical, updated_at = excluded.updated_at",
                (rule.rule_id, rule.version, dumps(rule.to_dict()), enabled_val, sc_val, now),
            )
            self._log_history(
                rule.rule_id,
                rule.version,
                rule.to_dict(),
                "remote_update" if remote else "local_apply",
                actor,
                "applied",
                "",
                now,
            )
        return True, "applied"

    def _log_history(
        self,
        rule_id: str,
        version: int | None,
        definition: dict[str, Any] | None,
        action: str,
        actor: str,
        outcome: str,
        reason: str,
        at: str,
    ) -> None:
        self.db.execute(
            "INSERT INTO rule_history (id, rule_id, version, definition, action, actor, "
            "outcome, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                rule_id,
                version,
                dumps(definition) if definition is not None else None,
                action,
                actor,
                outcome,
                reason,
                at,
            ),
        )

    def history(self, rule_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM rule_history WHERE rule_id = ? ORDER BY at, id", (rule_id,)
        )

    def get_state(self, rule_id: str, device_id: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT state FROM rule_state WHERE rule_id = ? AND device_id = ?",
            (rule_id, device_id),
        )
        return loads(row["state"]) if row else {"active": False}

    def set_state(self, rule_id: str, device_id: str, state: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO rule_state (rule_id, device_id, state, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (rule_id, device_id) DO UPDATE SET state = excluded.state, "
            "updated_at = excluded.updated_at",
            (rule_id, device_id, dumps(state), now_iso()),
        )


class RulesEngine:
    def __init__(self, store: EdgeStore, devices: DeviceStore, rules: RuleStore | None = None):
        self.store = store
        self.devices = devices
        self.rules = rules or RuleStore(store.db)

    def evaluate_event(self, event: CanonicalEvent) -> list[CanonicalEvent]:
        """Avalia regras aplicáveis ao evento canônico recebido; retorna alarmes gerados."""
        if event.event_type not in (EventType.TELEMETRY, EventType.POSITION):
            return []
        generated: list[CanonicalEvent] = []
        for rule in self.rules.list(enabled_only=True):
            if event.device_id not in rule.device_ids and "*" not in rule.device_ids:
                continue
            if rule.rule_type == "no_data":
                continue
            alarm = self._eval_rule(rule, event)
            if alarm is not None:
                generated.append(alarm)
        return generated

    def evaluate_no_data(self, now: datetime | None = None) -> list[CanonicalEvent]:
        """Avalia regras diagnósticas de ausência de telemetria (§15.2)."""
        now_dt = now or utc_now()
        generated: list[CanonicalEvent] = []
        for rule in self.rules.list(enabled_only=True):
            if rule.rule_type != "no_data":
                continue
            max_silence = float(rule.params["max_silence_s"])
            for device_id in rule.device_ids:
                device = self.devices.get(device_id)
                if device is None:
                    continue
                state = self.rules.get_state(rule.rule_id, device_id)
                was_active = bool(state.get("active", False))
                last_seen = device.last_seen_at
                if last_seen is None:
                    continue
                silence_s = (now_dt - parse_ts(last_seen)).total_seconds()
                is_silent = silence_s > max_silence
                if is_silent != was_active:
                    state["active"] = is_silent
                    state["last_change_at"] = iso(now_dt)
                    self.rules.set_state(rule.rule_id, device_id, state)
                    generated.append(
                        self._make_alarm(
                            rule=rule,
                            tenant_id=device.tenant_id,
                            site_id=device.site_id,
                            device_id=device_id,
                            observed_at=iso(now_dt),
                            active=is_silent,
                            trigger_event_id=f"silence:{last_seen}",
                            details={"silence_s": round(silence_s, 1), "max_silence_s": max_silence},
                        )
                    )
        return generated

    # ------------------------------------------------------------ avaliação
    def _valid_measurement(self, event: CanonicalEvent, name: str) -> float | bool | None:
        if name not in event.measurements:
            return None
        if event.quality.get(name, Quality.VALID.value) != Quality.VALID.value:
            return None
        return event.measurements[name]

    def _eval_rule(self, rule: Rule, event: CanonicalEvent) -> CanonicalEvent | None:
        state = self.rules.get_state(rule.rule_id, event.device_id)
        was_active = bool(state.get("active", False))
        p = rule.params
        new_active = was_active
        details: dict[str, Any] = {}

        if rule.rule_type == "bands":
            val = self._valid_measurement(event, p["measurement"])
            if not isinstance(val, int | float) or isinstance(val, bool):
                return None
            levels = p["levels"]  # lista ordenada crescente de {"name", "min", "severity"?}
            hyst = float(p.get("hysteresis", 0.0))
            prev_band = str(state.get("band", "NORMAL"))
            prev_idx = next((i for i, lv in enumerate(levels) if lv["name"] == prev_band), -1)

            # Determina nova faixa considerando histerese somente para rebaixamento
            new_idx = -1
            for i, lv in enumerate(levels):
                limit = float(lv["min"])
                effective = limit - hyst if i <= prev_idx else limit
                if float(val) >= effective:
                    new_idx = i

            new_band = levels[new_idx]["name"] if new_idx >= 0 else "NORMAL"
            if new_band == prev_band:
                return None
            state["band"] = new_band
            state["active"] = new_idx >= 0
            state["last_change_at"] = event.observed_at
            self.rules.set_state(rule.rule_id, event.device_id, state)
            sev = levels[new_idx].get("severity", rule.severity) if new_idx >= 0 else "info"
            return self._make_alarm(
                rule=rule,
                tenant_id=event.tenant_id,
                site_id=event.site_id,
                device_id=event.device_id,
                observed_at=event.observed_at,
                active=new_idx >= 0,
                trigger_event_id=event.event_id,
                details={
                    "measurement": p["measurement"],
                    "value": val,
                    "previous_band": prev_band,
                    "band": new_band,
                },
                severity_override=sev,
                transition_override=new_band.lower(),
            )

        if rule.rule_type == "threshold":
            val = self._valid_measurement(event, p["measurement"])
            if not isinstance(val, int | float) or isinstance(val, bool):
                return None
            op = p["operator"]
            limit = float(p["value"])
            hyst = float(p.get("hysteresis", 0.0))
            if not was_active:
                new_active = compare(float(val), op, limit)
            else:
                clear_limit = limit - hyst if op in (">", ">=") else limit + hyst
                new_active = compare(float(val), op, clear_limit)
            details = {"measurement": p["measurement"], "value": val, "threshold": limit}

        elif rule.rule_type == "battery":
            val = self._valid_measurement(event, p["measurement"])
            if not isinstance(val, int | float) or isinstance(val, bool):
                return None
            minimum = float(p["min"])
            hyst = float(p.get("hysteresis", 0.0))
            new_active = float(val) < minimum if not was_active else float(val) < (minimum + hyst)
            details = {"measurement": p["measurement"], "value": val, "min": minimum}

        elif rule.rule_type == "persistence":
            val = self._valid_measurement(event, p["measurement"])
            if not isinstance(val, int | float) or isinstance(val, bool):
                return None
            matched = compare(float(val), p["operator"], float(p["value"]))
            obs_dt = parse_ts(event.observed_at)
            if matched:
                since = state.get("condition_since")
                if not since:
                    state["condition_since"] = event.observed_at
                    self.rules.set_state(rule.rule_id, event.device_id, state)
                    return None
                elapsed = (obs_dt - parse_ts(since)).total_seconds()
                if elapsed >= float(p["duration_s"]):
                    new_active = True
                    details = {"measurement": p["measurement"], "value": val, "elapsed_s": elapsed}
            else:
                state.pop("condition_since", None)
                hyst = float(p.get("hysteresis", 0.0))
                op = p["operator"]
                limit = float(p["value"])
                clear_limit = limit - hyst if op in (">", ">=") else limit + hyst
                if not compare(float(val), op, clear_limit):
                    new_active = False
                    details = {"measurement": p["measurement"], "value": val}
                else:
                    self.rules.set_state(rule.rule_id, event.device_id, state)
                    return None

        elif rule.rule_type == "trend":
            name = p["measurement"]
            val = self._valid_measurement(event, name)
            if not isinstance(val, int | float) or isinstance(val, bool):
                return None
            window_s = float(p["window_s"])
            obs_dt = parse_ts(event.observed_at)
            since = iso(obs_dt - timedelta(seconds=window_s))
            points = self.store.recent_values(event.device_id, name, since, event.observed_at)
            if len(points) < 2:
                return None
            first_ts, first_val = points[0]
            last_ts, last_val = points[-1]
            dt_hours = (parse_ts(last_ts) - parse_ts(first_ts)).total_seconds() / 3600.0
            if dt_hours <= 0:
                return None
            rate_per_h = (last_val - first_val) / dt_hours
            min_rate = float(p["min_rate_per_h"])
            new_active = rate_per_h >= min_rate if min_rate >= 0 else rate_per_h <= min_rate
            details = {
                "measurement": name,
                "rate_per_h": round(rate_per_h, 4),
                "min_rate_per_h": min_rate,
            }

        elif rule.rule_type == "combination":
            results = []
            snapshot = {}
            for cond in p["conditions"]:
                val = self._valid_measurement(event, cond["measurement"])
                if not isinstance(val, int | float) or isinstance(val, bool):
                    return None
                snapshot[cond["measurement"]] = val
                results.append(compare(float(val), cond["operator"], float(cond["value"])))
            new_active = all(results)
            details = {"measurements": snapshot}

        elif rule.rule_type == "state":
            val = self._valid_measurement(event, p["measurement"])
            if val is None:
                return None
            is_on = bool(val)
            start_h, end_h = p["allowed_hours"]
            hour = parse_ts(event.observed_at).hour
            if start_h <= end_h:
                allowed = start_h <= hour < end_h
            else:
                allowed = hour >= start_h or hour < end_h
            new_active = is_on and not allowed
            details = {"measurement": p["measurement"], "value": is_on, "hour_utc": hour}

        if new_active == was_active:
            self.rules.set_state(rule.rule_id, event.device_id, state)
            return None

        state["active"] = new_active
        state["last_change_at"] = event.observed_at
        self.rules.set_state(rule.rule_id, event.device_id, state)
        return self._make_alarm(
            rule=rule,
            tenant_id=event.tenant_id,
            site_id=event.site_id,
            device_id=event.device_id,
            observed_at=event.observed_at,
            active=new_active,
            trigger_event_id=event.event_id,
            details=details,
        )

    @staticmethod
    def _make_alarm(
        rule: Rule,
        tenant_id: str,
        site_id: str,
        device_id: str,
        observed_at: str,
        active: bool,
        trigger_event_id: str,
        details: dict[str, Any],
        severity_override: str | None = None,
        transition_override: str | None = None,
    ) -> CanonicalEvent:
        transition = transition_override or ("raised" if active else "cleared")
        digest = hashlib.sha256(
            f"{rule.rule_id}|{rule.version}|{device_id}|{transition}|{trigger_event_id}".encode()
        ).hexdigest()[:20]
        now = now_iso()
        return CanonicalEvent(
            event_id=f"alarm:{rule.rule_id}:{digest}",
            tenant_id=tenant_id,
            site_id=site_id,
            device_id=device_id,
            event_type=EventType.EVENT.value,
            observed_at=observed_at,
            received_at=now,
            processed_at=now,
            measurements={"alarm_active_state": active},
            quality={
                "alarm_active_state": Quality.VALID.value,
                "timestamp_source": TimestampSource.EDGE.value,
            },
            source={"protocol": "internal", "adapter": "rules_engine", "rule_id": rule.rule_id},
            attributes={
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "rule_type": rule.rule_type,
                "severity": severity_override or rule.severity,
                "alarm_status": transition,
                "trigger_event_id": trigger_event_id,
                "details": details,
            },
        )
