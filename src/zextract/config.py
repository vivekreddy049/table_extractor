"""Frozen configuration.

The config object is hashed into ``extraction_runs.config_json`` so a run is
reproducible from its record alone. It is deliberately a plain nested mapping
wrapped in dotted accessors rather than a class hierarchy: it has to round-trip
through JSON byte-identically (DECISIONS.md, determinism table).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


class ConfigError(KeyError):
    """Raised when a config key is missing.

    Missing keys are a hard error rather than a silent default: a default that
    lives in code is exactly the absolute constant D20 forbids.
    """


@dataclass(frozen=True)
class Config:
    data: Mapping[str, Any]

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        with open(p, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not isinstance(raw, dict):
            raise ConfigError(f"config at {p} is not a mapping")
        return cls(data=raw)

    def get(self, dotted: str) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise ConfigError(f"missing config key: {dotted}")
            node = node[part]
        return node

    def f(self, dotted: str) -> float:
        return float(self.get(dotted))

    def i(self, dotted: str) -> int:
        return int(self.get(dotted))

    def b(self, dotted: str) -> bool:
        return bool(self.get(dotted))

    def to_json(self) -> str:
        return json.dumps(self.data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()
