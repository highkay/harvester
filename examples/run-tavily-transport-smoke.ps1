param(
    [int]$TimeoutSeconds = 120,
    [string]$LogLevel = "INFO",
    [int]$StatsInterval = 15,
    [string]$TokenFile = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path (Join-Path $repoRoot "main.py"))) {
    $repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
}

$configFile = Join-Path $repoRoot "examples\config-tavily-transport-smoke.yaml"
if (-not (Test-Path -LiteralPath $configFile)) {
    throw "Missing config: $configFile"
}

if (-not $TokenFile) {
    $candidates = @(
        (Join-Path $repoRoot "data\runs\provider-scan-20260721-tavily\github-token.env"),
        (Join-Path $repoRoot "github-token.env")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            $TokenFile = $c
            break
        }
    }
}

if (-not $TokenFile -or -not (Test-Path -LiteralPath $TokenFile)) {
    throw "Missing token file. Pass -TokenFile or set GITHUB_TOKENS env var."
}

$tokenLine = Get-Content -LiteralPath $TokenFile -Encoding UTF8 |
    Where-Object { $_ -match "^\s*GITHUB_TOKENS\s*=" } |
    Select-Object -First 1
if (-not $tokenLine) {
    throw "Token file must contain GITHUB_TOKENS=<token>"
}

$token = ($tokenLine -replace "^\s*GITHUB_TOKENS\s*=", "").Trim().Trim('"').Trim("'")
if ([string]::IsNullOrWhiteSpace($token) -or $token.StartsWith("your_")) {
    throw "GITHUB_TOKENS is empty or placeholder"
}

$previousToken = [Environment]::GetEnvironmentVariable("GITHUB_TOKENS", "Process")

try {
    [Environment]::SetEnvironmentVariable("GITHUB_TOKENS", $token, "Process")
    Push-Location $repoRoot
    Write-Host "Running Tavily transport smoke: config=$configFile timeout=${TimeoutSeconds}s"
    $pyArgs = @(
        "main.py",
        "-c", $configFile,
        "--log-level", $LogLevel,
        "--stats-interval", "$StatsInterval",
        "--timeout", "$TimeoutSeconds"
    )
    python @pyArgs
    $code = $LASTEXITCODE
    Write-Host "Exit code: $code"
    exit $code
}
finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable("GITHUB_TOKENS", $previousToken, "Process")
}
