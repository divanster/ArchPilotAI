<#
.SYNOPSIS
    Runs backend test files one by one against the local PostgreSQL test database.

.DESCRIPTION
    Builds the test database URL in-process from backend\.env (ARCHPILOT_DB_HOST, _PORT, _USER,
    _PASSWORD) with the database name replaced by -DatabaseName (default: archpilot_test). The URL
    and password are never printed. Before each file, every object in the PUBLIC schema of the test
    database is dropped (other schemas, roles and the database itself are untouched), because the
    `postgres_url` fixture requires an empty database and migrates it with Alembic. Each file is run
    with ARCHPILOT_REQUIRE_POSTGRES=1 so PostgreSQL tests fail instead of skipping. One summary line
    is written per file; full pytest output goes to a per-file log (credentials redacted).

    Refuses any database name that does not end in "_test", that equals ARCHPILOT_DB_NAME from
    backend\.env, or whose host is not local.

.EXAMPLE
    .\scripts\pg_validate.ps1 tests\test_migrations.py, tests\test_patches.py

.EXAMPLE
    .\scripts\pg_validate.ps1 -TestFiles (Get-ChildItem backend\tests\test_*.py).FullName
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$TestFiles,

    [string]$DatabaseName = "archpilot_test",

    [string]$LogDir = (Join-Path $env:TEMP "archpilot_pg_validate"),

    [string[]]$PytestArgs = @("-q", "-rs")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"
$envFile = Join-Path $backendDir ".env"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"
$localHosts = @("localhost", "127.0.0.1", "::1")

if (-not $DatabaseName.EndsWith("_test")) {
    throw "Refusing database '$DatabaseName': the name must end with '_test'."
}
if (-not (Test-Path -LiteralPath $envFile)) { throw "Missing $envFile" }
if (-not (Test-Path -LiteralPath $python)) { throw "Missing backend virtualenv python: $python" }

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
        if ($trimmed.StartsWith("export ")) { $trimmed = $trimmed.Substring(7).TrimStart() }
        $eq = $trimmed.IndexOf("=")
        if ($eq -lt 1) { continue }
        $key = $trimmed.Substring(0, $eq).Trim()
        $value = $trimmed.Substring($eq + 1).Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

function Get-Setting([hashtable]$Values, [string]$Key, [string]$Default = "", [switch]$Required) {
    if ($Values.ContainsKey($Key) -and $Values[$Key] -ne "") { return $Values[$Key] }
    if ($Required) { throw "backend\.env does not define $Key" }
    return $Default
}

$dotenv = Read-DotEnv $envFile
$dbHost = Get-Setting $dotenv "ARCHPILOT_DB_HOST" "localhost"
$dbPort = Get-Setting $dotenv "ARCHPILOT_DB_PORT" "5432"
$dbUser = Get-Setting $dotenv "ARCHPILOT_DB_USER" -Required
$dbPassword = Get-Setting $dotenv "ARCHPILOT_DB_PASSWORD"
$devDbName = Get-Setting $dotenv "ARCHPILOT_DB_NAME"

if ($localHosts -notcontains $dbHost) {
    throw "Refusing non-local database host '$dbHost' from backend\.env."
}
if ($devDbName -ne "" -and $devDbName -eq $DatabaseName) {
    throw "Refusing '$DatabaseName': it is the development database (ARCHPILOT_DB_NAME)."
}
if ($dbPort -notmatch '^\d+$') { throw "ARCHPILOT_DB_PORT in backend\.env is not a number." }

$urlHost = if ($dbHost -eq "::1") { "[::1]" } else { $dbHost }
$encodedUser = [uri]::EscapeDataString($dbUser)
$encodedPassword = [uri]::EscapeDataString($dbPassword)
$testUrl = "postgresql+psycopg://${encodedUser}:${encodedPassword}@${urlHost}:${dbPort}/${DatabaseName}"
$secrets = @(@($testUrl, $encodedPassword, $dbPassword) | Where-Object { $_ -ne "" } | Sort-Object Length -Descending -Unique)

function Hide-Secrets([string]$Text) {
    foreach ($secret in $secrets) { $Text = $Text.Replace($secret, "***") }
    return $Text
}

# Reads the URL from the environment; drops only objects in schema "public" of a *_test database.
$resetScript = @'
import os
import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

url = os.environ["ARCHPILOT_TEST_DATABASE_URL"]
parsed = make_url(url)
secret = parsed.password or ""


def fail(message: str) -> None:
    print(message.replace(secret, "***") if secret else message, file=sys.stderr)
    sys.exit(1)


if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").endswith("_test"):
    fail("refusing: not a PostgreSQL *_test database")
if parsed.host not in {"localhost", "127.0.0.1", "::1"}:
    fail("refusing: non-local host")

EXT = "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = {oid} AND d.deptype = 'e')"
RELATIONS = f"""
    SELECT c.relkind, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('m', 'v', 'f', 'p', 'r', 'S')
      AND {EXT.format(oid="c.oid")}
    ORDER BY CASE c.relkind WHEN 'm' THEN 0 WHEN 'v' THEN 1 WHEN 'f' THEN 2
                            WHEN 'p' THEN 3 WHEN 'r' THEN 4 ELSE 5 END, c.relname
"""
ROUTINES = f"""
    SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND {EXT.format(oid="p.oid")}
"""
TYPES = f"""
    SELECT t.typtype, t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd', 'r', 'm')
      AND {EXT.format(oid="t.oid")}
"""
DROP_RELATION = {
    "m": "MATERIALIZED VIEW", "v": "VIEW", "f": "FOREIGN TABLE",
    "p": "TABLE", "r": "TABLE", "S": "SEQUENCE",
}


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


try:
    engine = create_engine(url, connect_args={"connect_timeout": 10})
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '15s'"))
        for kind, name in conn.execute(text(RELATIONS)).all():
            conn.execute(text(f"DROP {DROP_RELATION[kind]} IF EXISTS public.{quote(name)} CASCADE"))
        for (signature,) in conn.execute(text(ROUTINES)).all():
            conn.execute(text(f"DROP ROUTINE IF EXISTS {signature} CASCADE"))
        for kind, name in conn.execute(text(TYPES)).all():
            keyword = "DOMAIN" if kind == "d" else "TYPE"
            conn.execute(text(f"DROP {keyword} IF EXISTS public.{quote(name)} CASCADE"))
    with engine.connect() as conn:
        remaining = inspect(conn).get_table_names(schema="public")
    engine.dispose()
except Exception as exc:  # noqa: BLE001
    fail(f"reset failed: {type(exc).__name__}: {exc}")
if remaining:
    fail(f"public schema still has tables: {remaining}")
print("public schema emptied")
'@

function Resolve-TestTarget([string]$Target) {
    $pathPart = ($Target -split "::", 2)[0]
    $suffix = $Target.Substring($pathPart.Length)
    foreach ($candidate in @($pathPart, (Join-Path $backendDir $pathPart), (Join-Path $repoRoot $pathPart))) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $full = (Resolve-Path -LiteralPath $candidate).Path
            if (-not $full.StartsWith($backendDir + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Test file '$Target' is not inside $backendDir"
            }
            return $full.Substring($backendDir.Length + 1) + $suffix
        }
    }
    throw "Test file not found: $Target"
}

$targets = @($TestFiles | ForEach-Object { Resolve-TestTarget $_ })
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$summaryPath = Join-Path $LogDir "summary-$stamp.txt"

$previousUrl = $env:ARCHPILOT_TEST_DATABASE_URL
$previousRequire = $env:ARCHPILOT_REQUIRE_POSTGRES
$failures = 0
Push-Location $backendDir
try {
    $env:ARCHPILOT_TEST_DATABASE_URL = $testUrl
    $env:ARCHPILOT_REQUIRE_POSTGRES = "1"

    foreach ($target in $targets) {
        $safeName = ($target -replace '[\\/:*?"<>|]', "_")
        $logPath = Join-Path $LogDir "$stamp-$safeName.log"

        # Native stderr must not become a terminating error under "Stop".
        $ErrorActionPreference = "Continue"
        $resetOutput = @($resetScript | & $python - 2>&1 | ForEach-Object { "$_" })
        $resetExit = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($resetExit -ne 0) {
            $reason = Hide-Secrets ($resetOutput | Select-Object -Last 1)
            $line = "{0,-50} RESET-FAILED  {1}" -f $target, $reason
            $failures++
        } else {
            $started = Get-Date
            $ErrorActionPreference = "Continue"
            $output = @(& $python -m pytest $target @PytestArgs 2>&1 | ForEach-Object { "$_" })
            $exitCode = $LASTEXITCODE
            $ErrorActionPreference = "Stop"
            $seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
            $text = Hide-Secrets ($output -join [Environment]::NewLine)
            [System.IO.File]::WriteAllText($logPath, $text)
            $final = $text -split "`r?`n" |
                Where-Object { $_ -match '\b(passed|failed|errors?|skipped|no tests ran|deselected)\b' } |
                Select-Object -Last 1
            if (-not $final) { $final = "(no pytest summary; see log)" }
            $status = if ($exitCode -eq 0) { "OK" } else { "FAIL($exitCode)" }
            if ($exitCode -ne 0) { $failures++ }
            $line = "{0,-50} {1,-13} {2,6}s  {3}  log={4}" -f $target, $status, $seconds, $final.Trim([char[]]" ="), $logPath
        }
        Write-Output $line
        Add-Content -LiteralPath $summaryPath -Value $line
    }
}
finally {
    Pop-Location
    $env:ARCHPILOT_TEST_DATABASE_URL = $previousUrl
    $env:ARCHPILOT_REQUIRE_POSTGRES = $previousRequire
    Remove-Variable testUrl, encodedPassword, dbPassword, secrets -ErrorAction SilentlyContinue
}

Write-Output "Summary: $($targets.Count - $failures)/$($targets.Count) files OK -> $summaryPath"
if ($failures -gt 0) { exit 1 }
