#Requires -Version 5.1

# ⚠ THIS FILE MUST KEEP ITS UTF-8 BOM. It contains non-ASCII characters (em
# dashes, ▶, ✔, box rules). Windows PowerShell 5.1 — which the #Requires above
# declares support for — reads a BOM-less .ps1 as the legacy ANSI codepage, so
# without the BOM every em dash decodes to mojibake and this script dies in a
# cascade of ParserError BEFORE RUNNING A SINGLE LINE. Worse, powershell.exe
# still exits 0, so the failure looks like a successful boot.
# If your editor offers to save as "UTF-8" vs "UTF-8 with BOM", choose WITH.
# Enforced by core/tests/unit/test_powershell_scripts_are_encoding_safe.py.

<#
.SYNOPSIS
    One-command quickstart: boot the whole recruiter-assistant stack in Docker,
    on UNIQUE host ports so it never collides with other apps on this machine.

.DESCRIPTION
    Automates the README "Quick start":
      1. Verifies Docker is running.
      2. Ensures a valid .env exists:
         - generates the REQUIRED PII_KEY / SKILL_HASH_SALT secrets if missing
           or blank (32 random bytes, base64, via the .NET CSPRNG — no openssl);
         - writes the project's UNIQUE host-port block (29xxx) if absent, so the
           stack never fights another app for 5432/6379/7474/7687/8000/5000.
           (docker-compose.yml reads these as ${X_PORT:-<stock>}; only the HOST
           side changes — in-network service DSNs are unchanged.)
      3. Ensures .env carries the inference config (LLM_BASE_URL / LLM_TIMEOUT_S)
         and checks that BOTH required models (gpt-oss:20b + nomic-embed-text)
         are reachable at LLM_BASE_URL — the shared Tailscale Ollama by default.
         Parsing/ranking stall without them, so this warns loudly if not.
      4. Preflights every required host port and fails with a clear
         "port N is held by <container>" message (not a raw Docker bind error)
         if a FOREIGN process already owns one.
      5. `docker compose up -d` for postgres · neo4j · redis · api · worker ·
         frontend. Schema + Neo4j vector indexes are created on API startup.
      6. Waits for the data tier + API /health to go green, then prints the URLs
         on their resolved ports.

    Offline-only by design: LLM_BASE_URL points at a local/tailnet Ollama; no
    candidate data ever leaves your machines. No cloud endpoints, ever.

.PARAMETER Build      Force a rebuild of the api/worker/frontend images (--build).
.PARAMETER NoCas      Boot WITHOUT CAS (dev-anonymous admin, no login screen).
                      CAS (SFU login + RBAC + user management) is ON by default.
.PARAMETER Down       Stop and remove the stack instead of starting it.
.PARAMETER Reset      With -Down, also delete the pg/neo4j volumes (down -v). DESTROYS data.
.PARAMETER Logs       After starting, follow the combined container logs.
.PARAMETER TimeoutSeconds  How long to wait for health. Default 180.

.EXAMPLE
    ./scripts/quickstart.ps1
.EXAMPLE
    ./scripts/quickstart.ps1 -Build -Logs
.EXAMPLE
    ./scripts/quickstart.ps1 -Down -Reset
#>
[CmdletBinding()]
param(
    [switch] $Build,
    [switch] $NoCas,
    [switch] $Down,
    [switch] $Reset,
    [switch] $Logs,
    [int]    $TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ── helpers ──────────────────────────────────────────────────────────────────
function Write-Step($msg)  { Write-Host "`n▶ $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  ✔ $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

function New-Base64Secret {
    # 32 cryptographically-random bytes, base64 — same shape as `openssl rand -base64 32`.
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes)
}

function Get-EnvValue($lines, $key, $default) {
    $line = $lines | Where-Object { $_ -match "^\s*$key\s*=" } | Select-Object -First 1
    if ($line) {
        $v = ($line -replace "^\s*$key\s*=", '').Trim()
        if (-not [string]::IsNullOrWhiteSpace($v)) { return $v }
    }
    return $default
}

# UNIQUE host-port scheme for this project (see .env.example / the unique-host-ports
# standing order). Only the HOST side; in-network ports stay stock.
$PortVars = [ordered]@{
    API_PORT        = 29800
    FRONTEND_PORT   = 29500
    POSTGRES_PORT   = 29432
    REDIS_PORT      = 29379
    NEO4J_HTTP_PORT = 29474
    NEO4J_BOLT_PORT = 29687
}

# Inference config written into .env if absent (matches .env.example). Inference
# runs on the GPU host aria-gb10 over Tailscale (its tailnet IP below; has
# gpt-oss:20b + nomic-embed-text) — your box must be on the tailnet. Only if you
# run your OWN Ollama on the app box instead: LLM_BASE_URL=
# http://host.docker.internal:11434/v1 + LLM_TIMEOUT_S=120 (+ pull the models).
$EnvDefaults = [ordered]@{
    LLM_BASE_URL  = 'http://100.88.247.106:11434/v1'   # aria-gb10 over Tailscale
    # 900, not 300. The two numbers are coupled and were set independently:
    # REASONING_JSON_MIN_TOKENS is 4096 (the only budget proven to make
    # gpt-oss:20b emit JSON at all), and at the ~23.5 tok/s this peer sustains
    # that is ~174s for ONE uncontended call. WorkerSettings.max_jobs is 4, so
    # four résumés parsing at once share the GPU and land at ~520-700s. At 300s
    # every one of them timed out, the circuit breaker opened, and parsing
    # stopped entirely (2026-08-21). Raising the token budget without raising
    # this trades empty responses for timeouts.
    LLM_TIMEOUT_S = '900'
    # CAS on by default. The comment below has claimed this since FU-5 and the
    # role keys above are generated BECAUSE of it — but CAS_ENABLED was never
    # actually written, so every quickstart produced an auth-DISABLED stack
    # while asserting the opposite, and the audit-log viewer was reachable with
    # no login. (docker-compose.yml had the mirror-image bug: it named
    # CAS_ENABLED only in a comment, so even a correct .env never reached the
    # containers. Fixed there with env_file.) Written only when ABSENT, so an
    # operator who deliberately set false keeps it.
    CAS_ENABLED           = 'true'
    CAS_SERVER_URL        = 'https://cas.sfu.ca/cas'
    CAS_SERVICE_BASE_URL  = 'http://localhost:29800'
    CAS_FRONTEND_BASE_URL = 'http://localhost:29500'
}

# Repo root = parent of this script's directory (scripts/quickstart.ps1).
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# ── docker-compose file set ──────────────────────────────────────────────────
# CAS (SFU login + RBAC + user management) is ON BY DEFAULT — the app is meant
# to run authenticated. Pass -NoCas to boot the dev-anonymous-admin passthrough.
$ComposeArgs = @('-f', 'docker-compose.yml')
$CasOn = $false
if (-not $NoCas) {
    if (Test-Path 'compose.cas.yml') { $ComposeArgs += @('-f', 'compose.cas.yml'); $CasOn = $true }
    else { Write-Warn2 'compose.cas.yml not found — booting WITHOUT CAS (dev-anonymous admin).' }
} else {
    Write-Warn2 'CAS disabled via -NoCas — dev-anonymous admin, no login screen.'
}

# ── 0. Docker up? ────────────────────────────────────────────────────────────
Write-Step 'Checking Docker'
try {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw 'docker info failed' }
} catch {
    Write-Error 'Docker does not appear to be running. Start Docker Desktop and retry.'
    exit 1
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Write-Error 'Docker Compose v2 ("docker compose") is required.'; exit 1 }
Write-Ok 'Docker is running.'

# ── Tear-down path ───────────────────────────────────────────────────────────
if ($Down) {
    Write-Step 'Stopping the stack'
    if ($Reset) {
        Write-Warn2 'Reset requested — this DELETES the Postgres/Neo4j volumes.'
        docker compose @ComposeArgs down -v
    } else {
        docker compose @ComposeArgs down
    }
    Write-Ok 'Stack stopped.'
    return
}

# ── 1. .env — required secrets + unique host ports + inference config ─────────
Write-Step 'Checking .env (secrets + unique host ports + inference config)'
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Ok 'Created .env from .env.example.'
}

$envLines = @(Get-Content '.env')
$changed = $false

# Secrets: generate if missing OR blank (compose hard-fails on `${PII_KEY:?...}`).
# The four role keys are included here too (security finding
# fix/auth-boundary-fails-open, F1b): CAS is on by default, and
# validate_startup_auth_config now REFUSES to boot with CAS enabled and all
# four empty, so a fresh quickstart boot needs at least one — generate all
# four so every role is independently keyed. API_KEY_RECRUITER is also the
# one key the Flask BFF presents on the browser's behalf
# (frontend/api_client.py::build_client) — without it the UI 401s once auth
# is on.
foreach ($key in @('PII_KEY', 'SKILL_HASH_SALT', 'API_KEY_ADMIN', 'API_KEY_RECRUITER', 'API_KEY_HIRING_MANAGER', 'API_KEY_AUDITOR')) {
    $line = $envLines | Where-Object { $_ -match "^\s*$key\s*=" } | Select-Object -First 1
    $value = if ($line) { ($line -replace "^\s*$key\s*=", '').Trim() } else { $null }
    if ([string]::IsNullOrWhiteSpace($value)) {
        $secret = New-Base64Secret
        if ($line) {
            $envLines = $envLines | ForEach-Object { if ($_ -match "^\s*$key\s*=") { "$key=$secret" } else { $_ } }
        } else { $envLines += "$key=$secret" }
        $changed = $true
        Write-Ok "Generated a random $key (32 bytes, base64)."
    }
}

# Host ports + inference config: write the default only if the key is ENTIRELY
# ABSENT (respect any value the user has already chosen).
foreach ($key in $PortVars.Keys) {
    $has = $envLines | Where-Object { $_ -match "^\s*$key\s*=" }
    if (-not $has) {
        $envLines += "$key=$($PortVars[$key])"
        $changed = $true
        Write-Ok "Set $key=$($PortVars[$key]) (unique host port)."
    }
}
foreach ($key in $EnvDefaults.Keys) {
    $has = $envLines | Where-Object { $_ -match "^\s*$key\s*=" }
    if (-not $has) {
        $envLines += "$key=$($EnvDefaults[$key])"
        $changed = $true
        Write-Ok "Set $key=$($EnvDefaults[$key])."
    }
}

if ($changed) {
    Set-Content -Path '.env' -Value $envLines -Encoding ASCII
    Write-Warn2 '.env holds secrets — it is gitignored; never commit it. Losing PII_KEY makes encrypted columns unrecoverable.'
} else {
    Write-Ok 'Secrets and host ports already set.'
}

# Resolve the ports actually in effect (user override in .env wins over the default).
$envLines     = @(Get-Content '.env')
$apiPort      = [int](Get-EnvValue $envLines 'API_PORT'        $PortVars['API_PORT'])
$frontendPort = [int](Get-EnvValue $envLines 'FRONTEND_PORT'   $PortVars['FRONTEND_PORT'])
$neo4jHttp    = [int](Get-EnvValue $envLines 'NEO4J_HTTP_PORT' $PortVars['NEO4J_HTTP_PORT'])
$resolved = [ordered]@{
    'api'          = $apiPort
    'frontend'     = $frontendPort
    'postgres'     = [int](Get-EnvValue $envLines 'POSTGRES_PORT'   $PortVars['POSTGRES_PORT'])
    'redis'        = [int](Get-EnvValue $envLines 'REDIS_PORT'      $PortVars['REDIS_PORT'])
    'neo4j-http'   = $neo4jHttp
    'neo4j-bolt'   = [int](Get-EnvValue $envLines 'NEO4J_BOLT_PORT' $PortVars['NEO4J_BOLT_PORT'])
}
$llmBase = Get-EnvValue $envLines 'LLM_BASE_URL' $EnvDefaults['LLM_BASE_URL']
$genModel = Get-EnvValue $envLines 'LLM_MODEL_GENERATION' 'gpt-oss:20b'
$embModel = Get-EnvValue $envLines 'LLM_MODEL_EMBEDDING'  'nomic-embed-text'

# The filesystem BlobStore bind-mounts ./data.
if (-not (Test-Path 'data')) { New-Item -ItemType Directory -Path 'data' | Out-Null }

# ── 2. Inference endpoint — must have BOTH models reachable at LLM_BASE_URL ────
Write-Step "Checking inference at LLM_BASE_URL ($llmBase)"
# Ollama's model list lives at the host root, not under /v1.
$tagsUrl = ($llmBase -replace '/v1/?$', '') + '/api/tags'
$needModels = @($genModel, $embModel)
try {
    $tags = Invoke-RestMethod -Uri $tagsUrl -TimeoutSec 6
    $have = @($tags.models | ForEach-Object { $_.name })
    $missing = $needModels | Where-Object { $m = $_; -not ($have | Where-Object { $_ -like "$m*" }) }
    if ($missing) {
        Write-Warn2 "Reachable, but missing model(s): $($missing -join ', '). Parsing/ranking will FAIL closed."
        Write-Warn2 "On that Ollama host, run:  ollama pull $($missing -join ' ')"
    } else { Write-Ok "Reachable with both models ($genModel, $embModel)." }
} catch {
    Write-Warn2 "Cannot reach the Ollama at $llmBase — the stack boots, but parsing/ranking FAIL closed until it's up."
    if ($llmBase -match '://100\.\d+\.\d+\.\d+') {
        Write-Warn2 'That is aria-gb10 over Tailscale — is THIS box joined to the tailnet and is aria-gb10 up?'
    }
    Write-Warn2 'Only if you run your OWN Ollama on this box instead: set LLM_BASE_URL='
    Write-Warn2 'http://host.docker.internal:11434/v1 + LLM_TIMEOUT_S=120 in .env, then'
    Write-Warn2 'ollama serve  and  ollama pull gpt-oss:20b nomic-embed-text'
}

# ── 3. Port preflight — clear message instead of a raw Docker bind error ──────
Write-Step 'Preflighting host ports'
$conflict = $false
foreach ($svc in $resolved.Keys) {
    $port = $resolved[$svc]
    $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        $holder = (docker ps --filter "publish=$port" --format '{{.Names}}' 2>$null | Select-Object -First 1)
        if ($holder -and $holder -like 'recruiter-assistant-*') {
            # Our own already-running container — up -d will reconcile it, not a conflict.
            continue
        }
        $by = if ($holder) { " by container '$holder'" } else { ' (non-Docker process)' }
        $var = ($PortVars.Keys | Where-Object { $resolved[$svc] -eq $PortVars[$_] } | Select-Object -First 1)
        if (-not $var) { $var = '<the matching *_PORT>' }
        Write-Warn2 ("Host port {0} ({1}) is already in use{2}. Change {3} in .env to a free port." -f $port, $svc, $by, $var)
        $conflict = $true
    }
}
if ($conflict) { Write-Error 'Resolve the port conflict(s) above (edit .env), then re-run.'; exit 1 }
Write-Ok 'All host ports free.'

# ── 4. Bring up the stack ────────────────────────────────────────────────────
Write-Step 'Starting containers (postgres · neo4j · redis · api · worker · frontend)'
$upArgs = @('up', '-d')
if ($Build) { $upArgs += '--build' }
docker compose @ComposeArgs @upArgs
if ($LASTEXITCODE -ne 0) { Write-Error 'docker compose up failed — see the output above.'; exit 1 }

# ── 5. Wait for health ───────────────────────────────────────────────────────
Write-Step "Waiting for the stack to become healthy (up to ${TimeoutSeconds}s)"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$apiOk = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:$apiPort/health" -TimeoutSec 3
        $status = if ($resp.PSObject.Properties.Name -contains 'status') { $resp.status } else { "$resp" }
        if ($status -eq 'ok') { $apiOk = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
}

Write-Host ''
docker compose @ComposeArgs ps

if ($apiOk) {
    Write-Host ''
    Write-Ok 'Stack is up.'
    Write-Host ''
    Write-Host ("  Frontend (recruiter UI) : http://localhost:{0}" -f $frontendPort) -ForegroundColor White
    Write-Host ("  API                     : http://localhost:{0}   (/health, /docs)" -f $apiPort) -ForegroundColor White
    Write-Host ("  Neo4j browser           : http://localhost:{0}   (neo4j / recruiterpass)" -f $neo4jHttp) -ForegroundColor White
    Write-Host ("  Inference (Ollama)      : {0}" -f $llmBase) -ForegroundColor White
    Write-Host ''
    if ($CasOn) {
        Write-Host ("  CAS login is ON — the browser will redirect to SFU CAS; first login as the" ) -ForegroundColor White
        Write-Host ("  default admin lands you as admin (RBAC + /admin/users). Boot with -NoCas to skip.") -ForegroundColor White
        Write-Host ''
    } else {
        Write-Warn2 'CAS is OFF (dev-anonymous admin) — no login, no user management UI. Drop -NoCas to enable.'
        Write-Host ''
    }
    Write-Host '  Logs : docker compose logs -f            Stop : ./scripts/quickstart.ps1 -Down' -ForegroundColor DarkGray
} else {
    Write-Warn2 "API /health did not go green within ${TimeoutSeconds}s. Inspect with:"
    Write-Host  '     docker compose logs api worker' -ForegroundColor DarkGray
    exit 1
}

# ── 6. Optional log follow ───────────────────────────────────────────────────
if ($Logs) {
    Write-Step 'Following logs (Ctrl-C detaches; the stack keeps running)'
    docker compose @ComposeArgs logs -f
}
