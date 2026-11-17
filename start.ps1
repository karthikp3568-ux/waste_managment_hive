param(
    [int]$HiveTimeoutSeconds = 240
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunDirectory = Join-Path $env:TEMP 'bengaluru-waste-intelligence'
$AppPidFile = Join-Path $RunDirectory 'flask.pid'
$StdoutLog = Join-Path $RunDirectory 'flask.stdout.log'
$StderrLog = Join-Path $RunDirectory 'flask.stderr.log'
$AppUrl = 'http://127.0.0.1:5000'

function Test-LocalPort([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch { return $false }
    finally { $client.Dispose() }
}

Write-Host '[1/4] Checking local prerequisites...'
$Docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $Docker) {
    Write-Error 'Docker CLI was not found. Install Docker Desktop, start it, then rerun: powershell -ExecutionPolicy Bypass -File .\start.ps1'
    exit 1
}
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        Write-Error 'Python was not found. Install Python 3.10 or newer, create the project .venv, and install requirements.txt.'
        exit 1
    }
    $Python = $PythonCommand.Source
}
& $Python -c "import flask, pandas, pyhive, sklearn"
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Required Python packages are missing. Install them with: python -m pip install -r requirements.txt, then rerun this launcher.'
    exit 1
}
if (Test-LocalPort 5000) {
    Write-Error 'Port 5000 is already occupied. Stop the existing Flask app (or identify the process with: Get-NetTCPConnection -LocalPort 5000) and rerun this launcher.'
    exit 1
}

Write-Host '[2/4] Starting Apache Hive with Docker Compose...'
Push-Location $ProjectRoot
try {
    & $Docker.Source compose up -d
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'Docker Compose could not start Hive. Confirm Docker Desktop is running and port 10000 is free; inspect details with: docker compose logs hive'
        exit 1
    }

    Write-Host "[3/4] Waiting up to $HiveTimeoutSeconds seconds for a real HiveQL SELECT 1 and initializing the Hive schema..."
    & $Python (Join-Path $ProjectRoot 'scripts\wait_for_hive.py') --timeout-seconds $HiveTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'Hive did not become query-ready. Inspect startup details with: docker compose logs --tail 100 hive. Flask was not started.'
        exit 1
    }

    Write-Host '[4/4] Starting Flask and waiting for its local HTTP endpoint...'
    New-Item -ItemType Directory -Force -Path $RunDirectory | Out-Null
    $env:PORT = '5000'
    $AppProcess = Start-Process -FilePath $Python -ArgumentList 'app.py' -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
    Set-Content -Path $AppPidFile -Value $AppProcess.Id -NoNewline
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        if ($AppProcess.HasExited) { break }
        try {
            $Response = Invoke-WebRequest -Uri $AppUrl -UseBasicParsing -TimeoutSec 2
            if ($Response.StatusCode -eq 200) { $Ready = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $Ready) {
        if (-not $AppProcess.HasExited) { Stop-Process -Id $AppProcess.Id -Force }
        Remove-Item -LiteralPath $AppPidFile -Force -ErrorAction SilentlyContinue
        Write-Error "Flask did not become available at $AppUrl. Check logs: $StdoutLog and $StderrLog"
        exit 1
    }
    Write-Host "Application is available at $AppUrl"
    Write-Host 'HiveServer2 passed HiveQL SELECT 1 and the application schema is initialized.'
    Write-Host 'Stop the application and Hive with: powershell -ExecutionPolicy Bypass -File .\stop.ps1'
    Start-Process $AppUrl
} finally {
    Pop-Location
}
