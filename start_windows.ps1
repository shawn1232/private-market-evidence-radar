$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$venvRoot = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $venvRoot "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$requirementsStamp = Join-Path $venvRoot ".requirements.sha256"
$logsDir = Join-Path $projectRoot "logs"
$browserRoot = Join-Path $projectRoot ".playwright-browsers"
$targetUrl = "http://127.0.0.1:8791/"

New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Initialize-PythonEnvironment {
    if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
        throw "requirements.txt is missing."
    }

    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($null -ne $pyLauncher) {
            & $pyLauncher.Source -3.12 -m venv $venvRoot
        } else {
            $basePython = Get-Command python.exe -ErrorAction SilentlyContinue
            if ($null -eq $basePython) {
                throw "Python 3.12 was not found. Install Python 3.12 and retry."
            }
            & $basePython.Source -m venv $venvRoot
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            throw "DealScope could not create its local Python environment."
        }
    }

    & $pythonPath -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "The existing .venv does not use Python 3.12 or newer. Recreate it with Python 3.12."
    }

    $requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $requirementsStamp) {
        (Get-Content -Raw -LiteralPath $requirementsStamp).Trim()
    } else {
        ""
    }
    if ($requirementsHash -ne $installedHash) {
        Invoke-Checked -FilePath $pythonPath `
            -Arguments @("-m", "pip", "install", "--upgrade", "pip") `
            -FailureMessage "DealScope could not update pip."
        Invoke-Checked -FilePath $pythonPath `
            -Arguments @("-m", "pip", "install", "-r", $requirementsPath) `
            -FailureMessage "DealScope could not install Python dependencies."
        Set-Content -LiteralPath $requirementsStamp -Value $requirementsHash -Encoding ASCII
    }

    $env:PLAYWRIGHT_BROWSERS_PATH = $browserRoot
    $browserPath = & $pythonPath -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()" 2>$null | Select-Object -Last 1
    if ([string]::IsNullOrWhiteSpace($browserPath) -or -not (Test-Path -LiteralPath $browserPath.Trim() -PathType Leaf)) {
        Invoke-Checked -FilePath $pythonPath `
            -Arguments @("-m", "playwright", "install", "chromium") `
            -FailureMessage "DealScope could not install Playwright Chromium."
    }
}

function Test-PortInUse {
    param([Parameter(Mandatory = $true)][int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(350)) {
            return $false
        }
        $client.EndConnect($pending)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-ServiceReady {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$ExpectedService
    )
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Method Get -TimeoutSec 2
        return [string]$health.service -eq $ExpectedService
    } catch {
        return $false
    }
}

function Wait-ServiceReady {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$ExpectedService,
        [int]$TimeoutSeconds = 75
    )
    for ($attempt = 0; $attempt -lt $TimeoutSeconds; $attempt++) {
        if (Test-ServiceReady -Port $Port -ExpectedService $ExpectedService) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Start-DealScopeService {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$ExpectedService,
        [Parameter(Mandatory = $true)][string]$RelativeScript
    )
    if (Test-ServiceReady -Port $Port -ExpectedService $ExpectedService) {
        return
    }
    if (Test-PortInUse -Port $Port) {
        throw "Port $Port is already used by another application."
    }

    $scriptPath = Join-Path $projectRoot $RelativeScript
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "DealScope service file is missing: $RelativeScript"
    }
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $stdoutLog = Join-Path $logsDir "${Name}_${stamp}.out.log"
    $stderrLog = Join-Path $logsDir "${Name}_${stamp}.err.log"
    $quotedScript = '"' + $scriptPath + '"'
    $process = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList $quotedScript `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    if (-not (Wait-ServiceReady -Port $Port -ExpectedService $ExpectedService)) {
        if ($process.HasExited) {
            throw "$Name exited during startup. See $stderrLog"
        }
        throw "$Name did not become ready. See $stderrLog"
    }
}

try {
    Initialize-PythonEnvironment
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:WEEKLY_RADAR_PORT = "8791"

    Start-DealScopeService -Name "dealscope_workbench" -Port 8787 -ExpectedService "DealScopeWorkbench" -RelativeScript "app\app.py"
    Start-DealScopeService -Name "dealscope_radar" -Port 8791 -ExpectedService "WeeklyProjectRadar" -RelativeScript "app\radar_app.py"

    Write-Host "DealScope is ready at $targetUrl"
    Start-Process -FilePath $targetUrl
    exit 0
} catch {
    Write-Error ("DealScope startup failed: {0}" -f $_.Exception.Message)
    exit 1
}
