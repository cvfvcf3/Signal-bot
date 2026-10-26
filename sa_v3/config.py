"""Config loader with bps-unit validation."""
from __future__ import annotations
import os
from typing import Any
import yaml

class ConfigError(ValueError):
    pass

def _walk_bps(obj: Any, path: str = "") -> None:
    if "parameter_space" in path: return
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}" if path else k
            if isinstance(k, str) and k.endswith("_bps"):
                if not isinstance(v, (int, float)):
                    raise ConfigError(f"{sub} must be numeric (bps)")
                if v < 0:
                    raise ConfigError(f"{sub} cannot be negative (bps)")
                if v > 10_000:
                    raise ConfigError(f"{sub} = {v} bps > 100%. Did you mean {v/100:.2f} bps?")
            else:
                _walk_bps(v, sub)
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            _walk_bps(x, f"{path}[{i}]")

def _validate_weights(config: dict) -> None:
    for mode_name, mode_cfg in config["modes"].items():
        weights = mode_cfg.get("weights", {})
        total = sum(weights.values())
        if abs(total - 100) > 0.5:
            raise ConfigError(f"modes.{mode_name}.weights sums to {total}, must be 100")

def load_config(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, "r") as f:
        config = yaml.safe_load(f)

    required = ["data", "symbols", "btc_symbol", "costs", "risk",
                "adaptive_target", "btc_macro", "modes", "active_modes",
                "layers", "walkforward", "parameter_space", "paper"]
    for key in required:
        if key not in config:
            raise ConfigError(f"Missing required config section: {key}")

    if os.environ.get("DATA_EXCHANGE"):
        config["data"]["exchange"] = os.environ["DATA_EXCHANGE"]
    if os.environ.get("DATA_DIR"):
        config["data"]["cache_dir"] = os.path.join(os.environ["DATA_DIR"], "candles")

    _walk_bps(config)
    _validate_weights(config)
    return config
