from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE = PROJECT_ROOT / "scripts" / "lib" / "LocalDevTools.psm1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "dev_tooling"
DEV_COMPOSE = PROJECT_ROOT / "docker-compose.dev.yml"


def _quoted(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _powershell(script: str, *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        pytest.skip("PowerShell is unavailable")
    arguments = [POWERSHELL, "-NoProfile", "-NonInteractive"]
    if Path(POWERSHELL).name.casefold() == "powershell.exe":
        arguments += ["-ExecutionPolicy", "Bypass"]
    arguments += ["-Command", script]
    return subprocess.run(
        arguments,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _module_script(body: str) -> str:
    return (
        "$ErrorActionPreference='Stop'; "
        f"Import-Module {_quoted(MODULE)} -Force; "
        + body
    )


def test_local_env_parser_handles_comments_blanks_equals_and_preserves_process_value(
) -> None:
    env_file = FIXTURES / "local.env"
    result = _powershell(
        _module_script(
            "$env:EXISTING_VALUE='original'; "
            f"$parsed=ConvertFrom-UwoEnvFile -Path {_quoted(env_file)}; "
            f"$loaded=Import-UwoLocalEnvironment -Path {_quoted(env_file)}; "
            "@{equals=($parsed.SIGNED_VALUE -eq 'alpha=beta=gamma'); "
            "existing=$env:EXISTING_VALUE; loaded=@($loaded.LoadedKeys); "
            "preserved=@($loaded.PreservedKeys)} | ConvertTo-Json -Compress"
        )
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["equals"] is True
    assert payload["existing"] == "original"
    assert "EXISTING_VALUE" in payload["preserved"]


def test_local_env_parser_rejects_malformed_lines() -> None:
    result = _powershell(
        _module_script(
            f"ConvertFrom-UwoEnvFile -Path {_quoted(FIXTURES / 'malformed.env')}"
        )
    )
    assert result.returncode != 0
    assert "expected KEY=VALUE" in result.stderr


def test_local_env_import_does_not_print_secrets() -> None:
    fixture_value = "fixture-value-must-not-appear"
    result = _powershell(
        _module_script(
            f"$loaded=Import-UwoLocalEnvironment -Path {_quoted(FIXTURES / 'secret.env')}; "
            "$loaded.LoadedKeys | ConvertTo-Json -Compress"
        )
    )
    assert result.returncode == 0, result.stderr
    assert fixture_value not in result.stdout
    assert fixture_value not in result.stderr
    assert "LOCAL_TOOL_SECRET" in result.stdout


@pytest.mark.parametrize(
    "url, expected",
    [
        ("postgresql://dev:hidden@example.com/uwo_housing_dev", "localhost"),
        (
            "postgresql://dev:hidden@project.supabase.co/uwo_housing_dev",
            "Supabase",
        ),
        ("postgresql://dev:hidden@127.0.0.1/another_database", "uwo_housing_dev"),
    ],
)
def test_development_database_guard_rejects_unsafe_targets(
    url: str, expected: str
) -> None:
    result = _powershell(
        _module_script(f"Assert-UwoDevelopmentDatabaseUrl -Url '{url}'")
    )
    assert result.returncode != 0
    assert expected.casefold() in result.stderr.casefold()
    assert "hidden" not in result.stderr


def test_test_database_builder_produces_a_postgresql_loopback_url_without_printing_password() -> None:
    password = "test-password-not-for-output"
    result = _powershell(
        _module_script(
            f"$url=New-UwoPostgresUrl -User test_user -Password '{password}' "
            "-Database uwo_housing_test -Port 55601; $parsed=[Uri]$url; "
            "@{scheme=$parsed.Scheme; host=$parsed.Host; database=$parsed.AbsolutePath; "
            "has_credentials=[bool]$parsed.UserInfo} | ConvertTo-Json -Compress"
        )
    )
    assert result.returncode == 0, result.stderr
    assert password not in result.stdout
    payload = json.loads(result.stdout.strip())
    assert payload == {
        "database": "/uwo_housing_test",
        "has_credentials": True,
        "host": "127.0.0.1",
        "scheme": "postgresql",
    }


def test_container_environment_parser_keeps_docker_entries_distinct_and_secret_safe() -> None:
    password = "container-password-must-not-appear=still-secret"
    result = _powershell(
        _module_script(
            f"$environment=ConvertTo-UwoContainerEnvironment -Entries @('POSTGRES_DB=uwo_housing_dev', 'POSTGRES_USER=uwo', 'POSTGRES_PASSWORD={password}', 'MALFORMED', 'ALMOST_POSTGRES_USER=wrong'); "
            "@{type=$environment.GetType().FullName; db=$environment['POSTGRES_DB']; "
            "user=$environment['POSTGRES_USER']; password_found=($environment['POSTGRES_PASSWORD'] -eq 'container-password-must-not-appear=still-secret'); "
            "missing=($null -eq $environment['MISSING']); malformed=($null -eq $environment['MALFORMED']); "
            "exact=($environment['POSTGRES_USER'] -ne $environment['ALMOST_POSTGRES_USER'])} | ConvertTo-Json -Compress"
        )
    )
    assert result.returncode == 0, result.stderr
    assert password not in result.stdout
    assert password not in result.stderr
    assert json.loads(result.stdout.strip()) == {
        "db": "uwo_housing_dev",
        "exact": True,
        "malformed": True,
        "missing": True,
        "password_found": True,
        "type": "System.Collections.Hashtable",
        "user": "uwo",
    }


def test_postgres_url_escapes_each_component_once_without_printing_password() -> None:
    password = "p@ss:/#?=%value"
    result = _powershell(
        _module_script(
            f"$url=New-UwoPostgresUrl -User 'user@name' -Password '{password}' -Database 'uwo housing' -Port 55600; "
            "$parsed=[Uri]$url; @{host=$parsed.Host; port=$parsed.Port; path=$parsed.AbsolutePath; "
            "user_info=$parsed.UserInfo; raw_url_has_encoded_password=($url -match '%40') } | ConvertTo-Json -Compress"
        )
    )
    assert result.returncode == 0, result.stderr
    assert password not in result.stdout
    assert password not in result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 55600
    assert payload["path"] == "/uwo%20housing"
    assert payload["raw_url_has_encoded_password"] is True
    assert "%2540" not in payload["user_info"]


def test_local_api_launcher_initializes_database_before_uvicorn() -> None:
    launcher = (PROJECT_ROOT / "scripts" / "serve_local_api.ps1").read_text(
        encoding="utf-8"
    )
    assert "Initialize-UwoLocalEnvironment" in launcher
    assert "-RequireDatabase" in launcher
    assert 'SetEnvironmentVariable("DATABASE_URL", $localDatabaseUrl, "Process")' in launcher
    assert "backend.main:app" in launcher


def test_latest_accessibility_run_requires_manifest_and_uses_started_at(
) -> None:
    result = _powershell(
        _module_script(
            f"$run=Get-UwoLatestAccessibilityRun -RunRoot {_quoted(FIXTURES / 'runs')}; $run.RunId"
        )
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "newer"


def test_fast_plan_does_not_launch_postgres_and_full_plan_guarantees_cleanup() -> None:
    fast = _powershell(
        f"& {_quoted(PROJECT_ROOT / 'scripts' / 'test.ps1')} fast -PlanOnly"
    )
    full = _powershell(
        f"& {_quoted(PROJECT_ROOT / 'scripts' / 'test.ps1')} full -PlanOnly"
    )
    assert fast.returncode == 0, fast.stderr
    assert "PostgreSQL" not in fast.stdout
    assert full.returncode == 0, full.stderr
    assert "PostgreSQL cleanup: ALWAYS" in full.stdout
    assert "without --volumes" in full.stdout


def test_routing_smoke_refuses_missing_local_config() -> None:
    result = _powershell(
        f"& {_quoted(PROJECT_ROOT / 'scripts' / 'routing.ps1')} smoke "
        f"-Config {_quoted(FIXTURES / 'missing.toml')}"
    )
    assert result.returncode != 0
    assert "Local accessibility worker config is missing" in result.stderr


def test_routing_arguments_preserve_worker_limits() -> None:
    result = _powershell(
        _module_script(
            "$args=Get-UwoRoutingWorkerArguments -Command run -ConfigPath config.toml "
            "-PropertiesPath properties.csv -PropertyIds 2,3,4; $args -join ' '"
        )
    )
    assert result.returncode == 0, result.stderr
    assert "scripts.run_accessibility_worker run" in result.stdout
    assert "--property-ids 2,3,4" in result.stdout
    assert "--allow-larger-run" not in result.stdout


def test_stop_script_is_non_destructive_and_pipeline_entry_points_are_unchanged() -> None:
    dev_script = (PROJECT_ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    routing_script = (PROJECT_ROOT / "scripts" / "routing.ps1").read_text(
        encoding="utf-8"
    )
    module = MODULE.read_text(encoding="utf-8")
    assert "--volumes" not in dev_script
    assert "docker rm" not in dev_script
    assert "volume rm" not in dev_script
    assert "scripts.run_accessibility_worker" in module
    assert "Get-UwoRoutingWorkerArguments" in routing_script
    assert "--allow-larger-run" not in routing_script
    assert '$row.reason_codes -or $row.decision -eq "accepted_with_warning"' in routing_script


def test_development_compose_is_loopback_only_and_persistent() -> None:
    compose = DEV_COMPOSE.read_text(encoding="utf-8")
    assert '"127.0.0.1:55600:5432"' in compose
    assert '"127.0.0.1:8080:8080"' in compose
    assert re.search(
        r"uwo-postgres-dev-data:/var/lib/postgresql/data", compose
    )
    assert re.search(
        r"uwo-postgres-dev-data:\s*\n\s+name: uwo-postgres-dev-data\s*\n\s+external: true",
        compose,
    )
    assert 'source: ./data/routing' in compose
    assert 'target: /var/opentripplanner' in compose
    assert 'read_only: true' in compose
    assert 'command: ["--load", "--serve"]' in compose
    assert "--build" not in compose
    assert "0.0.0.0:" not in compose


def test_development_compose_contains_no_tracked_password_or_production_target() -> None:
    compose = DEV_COMPOSE.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" in compose
    assert not re.search(r"POSTGRES_PASSWORD:\s+(?!\$\{)", compose)
    assert "supabase" not in compose.casefold()
    assert "production" not in compose.casefold()
    assert "service_role" not in compose.casefold()


def test_dev_lifecycle_uses_canonical_compose_and_blocks_legacy_conflicts() -> None:
    dev_script = (PROJECT_ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    assert '$DevCompose = Join-Path $ProjectRoot "docker-compose.dev.yml"' in dev_script
    assert '@("compose", "-f", $DevCompose, "up", "-d")' in dev_script
    assert '@("compose", "-f", $DevCompose, "stop")' in dev_script
    assert "Get-UwoLegacyDevelopmentContainers" in dev_script
    assert "No containers were changed" in dev_script
    assert 'Invoke-UwoDocker -Arguments @("rm"' not in dev_script
    assert "down -v" not in dev_script
    assert "--volumes" not in dev_script


def test_dev_stop_loads_compose_password_and_does_not_report_failed_stop_as_success() -> None:
    dev_script = (PROJECT_ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    stop_function = dev_script.split("function Stop-DevelopmentEnvironment", 1)[1].split(
        "function Invoke-Doctor", 1
    )[0]
    compose_stop = '@("compose", "-f", $DevCompose, "stop")'
    assert stop_function.index("Import-UwoLocalEnvironment") < stop_function.index(
        compose_stop
    )
    assert '"compose-stop-placeholder"' in stop_function
    assert 'throw "Could not stop canonical development services' in stop_function
    assert 'Write-Warning "Could not stop canonical development services' not in stop_function


def test_doctor_binding_guard_detects_wildcard_exposure() -> None:
    result = _powershell(
        _module_script(
            "@{loopback=(Test-UwoLoopbackBindings @('127.0.0.1')); "
            "wildcard=(Test-UwoLoopbackBindings @('0.0.0.0')); "
            "empty=(Test-UwoLoopbackBindings @())} | ConvertTo-Json -Compress"
        )
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "empty": False,
        "loopback": True,
        "wildcard": False,
    }


def test_disposable_test_database_automation_remains_separate() -> None:
    test_script = (PROJECT_ROOT / "scripts" / "test.ps1").read_text(encoding="utf-8")
    assert 'docker-compose.postgres-test.yml' in test_script
    assert 'TEST_DATABASE_URL' in test_script
    assert 'approve_test_database_environment' in test_script
    assert 'postgres and not r5' in test_script
    assert 'docker-compose.dev.yml' not in test_script
    assert 'ACCESSIBILITY_DATABASE_URL' not in test_script


def test_doctor_and_redaction_helpers_do_not_reveal_credentials() -> None:
    secret = "doctor-secret-must-not-appear"
    redacted = _powershell(
        _module_script(
            f"Protect-UwoDiagnosticText 'failed postgresql://dev:{secret}@127.0.0.1:55600/uwo_housing_dev'"
        )
    )
    result = _powershell(
        f"& {_quoted(PROJECT_ROOT / 'scripts' / 'dev.ps1')} doctor "
        f"-EnvFile {_quoted(PROJECT_ROOT / 'config' / 'local-dev.example.env')}",
        timeout=45,
    )
    assert redacted.returncode == 0, redacted.stderr
    assert secret not in redacted.stdout
    assert "<redacted-database-url>" in redacted.stdout
    assert "postgresql://" not in result.stdout
    assert "postgresql://" not in result.stderr
    assert "Validated without displaying values" in result.stdout
