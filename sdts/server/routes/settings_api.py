"""`/api/config`: what the forms need to render, and the YAML they can edit.

Split in two on purpose:

* `GET /api/config/defaults` returns the dataclass defaults as JSON so the
  browser never hardcodes a threshold that drifts from `config.py`.
* `GET|PUT /api/config/file` reads and writes a YAML file the user names.

The write path is the one place where arbitrary file content could reach
disk, so it is restricted to `*.yaml` / `*.yml` and validated by loading it
through `PipelineConfig.from_dict` - which rejects unknown keys - before
anything is written.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Body, HTTPException, Query, Request

from ...config import PipelineConfig
from ..deps import settings

router = APIRouter()


def _defaults_dict() -> dict[str, Any]:
    cfg = PipelineConfig()
    data = cfg.to_dict()
    data.pop("hf_token", None)          # never echo a credential to the browser
    return data


@router.get("/defaults")
def defaults(request: Request):
    st = settings(request)
    cfg = _defaults_dict()
    if st.base_config_path and Path(st.base_config_path).is_file():
        try:
            cfg = PipelineConfig.load(st.base_config_path).to_dict()
            cfg.pop("hf_token", None)
        except (OSError, ValueError, yaml.YAMLError):
            pass                        # fall back to dataclass defaults
    return {
        "config": cfg,
        "base_config": st.base_config_path,
        "data_dir": str(st.data_dir),
        "registry": st.registry_path,
        "version": st.version,
    }


@router.get("/file")
def read_file(request: Request, path: str = Query(...)):
    target = _yaml_target(request, path, must_exist=True)
    cfg = PipelineConfig.load(target)
    data = cfg.to_dict()
    data.pop("hf_token", None)
    return {"path": str(target), "config": data}


@router.put("/file")
def write_file(request: Request, body: dict[str, Any] = Body(...)):
    path = body.get("path")
    config = body.get("config")
    if not isinstance(path, str) or not path.strip():
        raise HTTPException(400, "`path` is required")
    if not isinstance(config, dict):
        raise HTTPException(400, "`config` must be an object")

    target = _yaml_target(request, path, must_exist=False)
    # Validate before writing: from_dict raises on unknown keys and on a bad
    # enum, so a typo in the form becomes a 400 instead of a config the CLI
    # will refuse to load later.
    try:
        cfg = PipelineConfig.from_dict(config)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"invalid config: {exc}") from None
    serialised = cfg.to_dict()
    serialised.pop("hf_token", None)
    # Round-trip guard: dumping the dataclasses keeps tuples as tuples, which
    # PyYAML serialises as `!!python/tuple`. Convert to plain lists first.
    plain = _to_plain(serialised)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(plain, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"path": str(target), "config": plain}


def _to_plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _to_plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def _yaml_target(request: Request, raw: str, *, must_exist: bool) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(settings(request).data_dir).parent / path
    path = path.resolve()
    if path.suffix.lower() not in (".yaml", ".yml"):
        raise HTTPException(400, "config files must be .yaml or .yml")
    if must_exist and not path.is_file():
        raise HTTPException(404, f"no such config file: {path}")
    return path
