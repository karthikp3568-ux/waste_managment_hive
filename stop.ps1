$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppPidFile = Join-Path $env:TEMP 'bengaluru-waste-intelligence\flask.pid'

if (Test-Path $AppPidFile) {
    $StoredPid = 0
    if ([int]::TryParse((Get-Content -Raw -LiteralPath $AppPidFile), [ref]$StoredPid)) {
        $Target = Get-CimInstance Win32_Process -Filter "ProcessId = $StoredPid" -ErrorAction SilentlyContinue
        if ($Target -and $Target.CommandLine -match '(?i)(^|\s)app\.py($|\s)') {
            Stop-Process -Id $StoredPid -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped Flask process $StoredPid."
        } else {
            Write-Host 'No matching Flask process was found; leaving unrelated processes untouched.'
        }
    }
    Remove-Item -LiteralPath $AppPidFile -Force -ErrorAction SilentlyContinue
} else {
    Write-Host 'No launcher-managed Flask process was recorded.'
}

$Docker = Get-Command docker -ErrorAction SilentlyContinue
if ($Docker) {
    Push-Location $ProjectRoot
    try {
        & $Docker.Source compose down
        if ($LASTEXITCODE -eq 0) { Write-Host 'Stopped the Compose Hive service. The hive-warehouse volume was preserved.' }
        else { Write-Host 'Could not stop Hive through Docker Compose. Check Docker Desktop and run: docker compose down' }
    } finally { Pop-Location }
} else {
    Write-Host 'Docker CLI is unavailable; Hive was not stopped. Start Docker Desktop, then run: docker compose down'
}
