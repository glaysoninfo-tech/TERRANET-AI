"""Contrato de regras de automação versionadas (PIT-001 §15.3)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from contracts.canonical import is_valid_measurement_name

RULE_TYPES = frozenset(
    {"threshold", "trend", "persistence", "combination", "state", "no_data", "battery"}
)
OPERATORS = frozenset({">", ">=", "<", "<="})
SEVERITIES = frozenset({"info", "warning", "critical"})


class RuleValidationError(ValueError):
    pass


def compare(value: float, operator: str, reference: float) -> bool:
    return {
        ">": value > reference,
        ">=": value >= reference,
        "<": value < reference,
        "<=": value <= reference,
    }[operator]


@dataclass
class Rule:
    rule_id: str
    version: int
    rule_type: str
    params: dict[str, Any]
    device_ids: list[str] = field(default_factory=list)
    enabled: bool = True
    author: str = ""
    approved_at: str = ""
    severity: str = "warning"
    safety_critical: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        known = set(cls.__dataclass_fields__)
        rule = cls(**{k: v for k, v in data.items() if k in known})
        rule.validate()
        return rule

    def _measurement(self, params: dict[str, Any], key: str = "measurement") -> None:
        name = params.get(key)
        if not isinstance(name, str) or not is_valid_measurement_name(name):
            raise RuleValidationError(f"{self.rule_id}: medição inválida '{name}'")

    @staticmethod
    def _number(params: dict[str, Any], key: str, minimum: float | None = None) -> None:
        value = params.get(key)
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise RuleValidationError(f"parâmetro numérico obrigatório: {key}")
        if minimum is not None and value < minimum:
            raise RuleValidationError(f"parâmetro {key} deve ser >= {minimum}")

    def validate(self) -> None:
        if not self.rule_id or not isinstance(self.rule_id, str):
            raise RuleValidationError("rule_id obrigatório")
        if not isinstance(self.version, int) or self.version < 1:
            raise RuleValidationError("version deve ser inteiro >= 1")
        if self.rule_type not in RULE_TYPES:
            raise RuleValidationError(f"tipo de regra desconhecido: {self.rule_type}")
        if self.severity not in SEVERITIES:
            raise RuleValidationError(f"severidade inválida: {self.severity}")
        if not isinstance(self.params, dict):
            raise RuleValidationError("params deve ser objeto")
        p = self.params
        if self.rule_type in ("threshold", "persistence"):
            self._measurement(p)
            if p.get("operator") not in OPERATORS:
                raise RuleValidationError("operator inválido")
            self._number(p, "value")
            if "hysteresis" in p:
                self._number(p, "hysteresis", 0)
            if self.rule_type == "persistence":
                self._number(p, "duration_s", 1)
        elif self.rule_type == "battery":
            self._measurement(p)
            self._number(p, "min")
            if "hysteresis" in p:
                self._number(p, "hysteresis", 0)
        elif self.rule_type == "trend":
            self._measurement(p)
            self._number(p, "window_s", 60)
            self._number(p, "min_rate_per_h")
        elif self.rule_type == "combination":
            conditions = p.get("conditions")
            if not isinstance(conditions, list) or len(conditions) < 2:
                raise RuleValidationError("combination exige ao menos 2 condições")
            for cond in conditions:
                self._measurement(cond)
                if cond.get("operator") not in OPERATORS:
                    raise RuleValidationError("operator inválido")
                self._number(cond, "value")
        elif self.rule_type == "state":
            self._measurement(p)
            hours = p.get("allowed_hours")
            if (
                not isinstance(hours, list)
                or len(hours) != 2
                or not all(isinstance(h, int) and 0 <= h <= 24 for h in hours)
            ):
                raise RuleValidationError("allowed_hours deve ser [inicio, fim] em horas")
        elif self.rule_type == "no_data":
            self._number(p, "max_silence_s", 60)
        if not self.device_ids:
            raise RuleValidationError("regra deve estar associada a equipamentos")
