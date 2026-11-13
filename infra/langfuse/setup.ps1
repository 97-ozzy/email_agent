# Generate independent local infrastructure secrets, without overwriting an existing setup.
$ErrorActionPreference = "Stop"
$settingsPath = Join-Path $PSScriptRoot ".env"
if (Test-Path -LiteralPath $settingsPath) {
    Write-Host "Langfuse infrastructure .env already exists; preserved."
    exit 0
}
$lines = @("NEXTAUTH_URL=http://localhost:3000")
foreach ($name in @("POSTGRES_PASSWORD", "SALT", "ENCRYPTION_KEY", "CLICKHOUSE_PASSWORD", "REDIS_AUTH", "MINIO_ROOT_PASSWORD", "NEXTAUTH_SECRET")) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $value = ([BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
    $lines += "$name=$value"
}
$lines | Set-Content -LiteralPath $settingsPath -Encoding ascii
Write-Host "Created local Langfuse infrastructure .env."
