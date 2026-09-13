"""Configuration loading and validation for the remote pipeline operator."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
import tomllib
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "operator.toml"
_SAFE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class OperatorConfigError(ValueError):
    """Operator configuration is missing or unsafe."""


@dataclass(frozen=True)
class RemoteConfig:
    host: str = ""
    project_path: str = r"C:\path\to\uwo-housing-website-project"
    python_path: str = r".venv-scraperai\Scripts\python.exe"
    powershell: str = "powershell.exe"
    ollama_endpoint: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:14b-instruct"
    minimum_free_gb: float = 5.0


@dataclass(frozen=True)
class LocalConfig:
    review_root: Path = PROJECT_ROOT / "data" / "remote-runs"


@dataclass(frozen=True)
class PipelineConfig:
    geocode_cache: str = r"data\processed\geocode_cache.csv"
    maximum_retries: int = 2
    retry_delay_seconds: float = 1.0


@dataclass(frozen=True)
class OperatorConfig:
    remote: RemoteConfig
    local: LocalConfig
    pipeline: PipelineConfig


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise OperatorConfigError(f"[{name}] must be a TOML table")
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as source:
            value = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise OperatorConfigError(f"Cannot read operator config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OperatorConfigError("Operator config must be a TOML object")
    return value


def validate_config(config: OperatorConfig) -> OperatorConfig:
    host = config.remote.host.strip()
    if not host:
        raise OperatorConfigError("remote host is required")
    if not _SAFE_HOST.fullmatch(host):
        raise OperatorConfigError("remote host must be a plain SSH alias or hostname")
    for label, value in (
        ("remote project path", config.remote.project_path),
        ("remote Python path", config.remote.python_path),
        ("PowerShell executable", config.remote.powershell),
        ("geocode cache", config.pipeline.geocode_cache),
    ):
        if not value or any(character in value for character in "\r\n\0"):
            raise OperatorConfigError(f"{label} is blank or unsafe")
    if Path(config.remote.powershell).name.casefold() != "powershell.exe":
        raise OperatorConfigError("remote PowerShell must be powershell.exe")
    if config.pipeline.maximum_retries < 0 or config.pipeline.maximum_retries > 10:
        raise OperatorConfigError("maximum_retries must be between 0 and 10")
    if config.pipeline.retry_delay_seconds < 0:
        raise OperatorConfigError("retry_delay_seconds must be non-negative")
    if config.remote.minimum_free_gb < 0:
        raise OperatorConfigError("minimum_free_gb must be non-negative")
    return replace(config, remote=replace(config.remote, host=host))


def load_config(
    *,
    config_path: Optional[Path] = None,
    host: Optional[str] = None,
    project_path: Optional[str] = None,
    python_path: Optional[str] = None,
    review_root: Optional[Path] = None,
) -> OperatorConfig:
    """Load defaults, then a local TOML file, then explicit CLI overrides."""

    path = (config_path or DEFAULT_CONFIG_PATH).resolve()
    data = _read_toml(path)
    remote_data = _table(data, "remote")
    local_data = _table(data, "local")
    pipeline_data = _table(data, "pipeline")
    remote = RemoteConfig(
        host=str(remote_data.get("host", "")),
        project_path=str(remote_data.get("project_path", RemoteConfig.project_path)),
        python_path=str(remote_data.get("python_path", RemoteConfig.python_path)),
        powershell=str(remote_data.get("powershell", RemoteConfig.powershell)),
        ollama_endpoint=str(
            remote_data.get("ollama_endpoint", RemoteConfig.ollama_endpoint)
        ),
        ollama_model=str(remote_data.get("ollama_model", RemoteConfig.ollama_model)),
        minimum_free_gb=float(
            remote_data.get("minimum_free_gb", RemoteConfig.minimum_free_gb)
        ),
    )
    configured_review_root = Path(
        str(local_data.get("review_root", "data/remote-runs"))
    )
    if not configured_review_root.is_absolute():
        configured_review_root = PROJECT_ROOT / configured_review_root
    pipeline = PipelineConfig(
        geocode_cache=str(
            pipeline_data.get("geocode_cache", PipelineConfig.geocode_cache)
        ),
        maximum_retries=int(
            pipeline_data.get("maximum_retries", PipelineConfig.maximum_retries)
        ),
        retry_delay_seconds=float(
            pipeline_data.get("retry_delay_seconds", PipelineConfig.retry_delay_seconds)
        ),
    )
    config = OperatorConfig(remote, LocalConfig(configured_review_root), pipeline)
    config = replace(
        config,
        remote=replace(
            config.remote,
            host=host if host is not None else config.remote.host,
            project_path=(
                project_path if project_path is not None else config.remote.project_path
            ),
            python_path=python_path if python_path is not None else config.remote.python_path,
        ),
        local=LocalConfig((review_root or config.local.review_root).resolve()),
    )
    return validate_config(config)
