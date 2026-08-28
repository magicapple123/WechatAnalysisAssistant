<#
.SYNOPSIS
Build and start the browser edition from a source checkout.

.DESCRIPTION
The script only stops a listener on port 8520 when it can prove that the
process is this checkout's virtual-environment Python running the project
backend. An unrelated process is never terminated.
#>
[CmdletBinding()]
param(
    [switch] $SkipFrontendBuild,
    [switch] $NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Port = 8520

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string] $Program,
        [Parameter(ValueFromRemainingArguments = $true)][string[]] $Arguments
    )

    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Program $($Arguments -join ' ')"
    }
}

function Get-ListeningProcessIds {
    try {
        return @(
            Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess -Unique
        )
    }
    catch [Microsoft.PowerShell.Cmdletization.Cim.CimJobException] {
        if ($_.FullyQualifiedErrorId -like "CmdletizationQuery_NotFound,*") {
            return @()
        }
        throw
    }
}

function Assert-SupportedPython {
    param([Parameter(Mandatory = $true)][string] $PythonPath)

    $versionText = & $PythonPath -c `
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to query Python version: $PythonPath"
    }
    $version = [Version]([string]$versionText).Trim()
    if ($version -lt [Version]"3.10.0") {
        throw "Python 3.10 or newer is required; $PythonPath reports $version. Remove .venv and rerun after installing a supported Python version."
    }
}

function Get-ContentHash {
    param([Parameter(Mandatory = $true)][string] $Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Stop-PreviousProjectServer {
    $owners = @(Get-ListeningProcessIds)
    foreach ($processId in $owners) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
        if (-not $processInfo) {
            # The listener exited between the socket and process queries.
            continue
        }
        $actualExecutable = [string]$processInfo.ExecutablePath
        $commandLine = [string]$processInfo.CommandLine
        $samePython = $actualExecutable -and (
            [System.IO.Path]::GetFullPath($actualExecutable) -ieq
                [System.IO.Path]::GetFullPath($VenvPython)
        )
        $expectedCommandLine = (
            '^\s*(?:"[^"]+"|\S+)\s+-m\s+backend\.main\s+' +
            '--no-browser\s+--port\s+' +
            [regex]::Escape([string]$Port) +
            '\s*$'
        )
        $sameBackend = $commandLine -match $expectedCommandLine

        if (-not ($samePython -and $sameBackend)) {
            throw "Port $Port is already used by PID $processId. Stop that program or choose another port; it was left untouched."
        }

        Write-Host "Stopping the previous project server (PID $processId)..."
        Stop-Process -Id $processId -Force -ErrorAction Stop
    }

    if ($owners.Count -gt 0) {
        $deadline = [DateTime]::UtcNow.AddSeconds(5)
        while ((Get-ListeningProcessIds).Count -gt 0) {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "The previous server did not release port $Port in time."
            }
            Start-Sleep -Milliseconds 150
        }
    }
}

function Initialize-PythonEnvironment {
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        Write-Host "Creating .venv..."
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            $candidateVersionText = & $pyLauncher.Source -3 -c `
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
            if ($LASTEXITCODE -ne 0) {
                throw "Unable to query the Python selected by py.exe -3."
            }
            $candidateVersion = [Version]([string]$candidateVersionText).Trim()
            if ($candidateVersion -lt [Version]"3.10.0") {
                throw "Python 3.10 or newer is required to create .venv."
            }
            Invoke-Checked $pyLauncher.Source -3 -m venv (Join-Path $ProjectRoot ".venv")
        }
        else {
            $python = Get-Command python.exe -ErrorAction Stop
            Assert-SupportedPython -PythonPath $python.Source
            Invoke-Checked $python.Source -m venv (Join-Path $ProjectRoot ".venv")
        }
    }

    Assert-SupportedPython -PythonPath $VenvPython
    $requirementsPath = Join-Path $ProjectRoot "backend\requirements.txt"
    $requirementsStamp = Join-Path $ProjectRoot ".venv\.wechat-analysis-requirements.sha256"
    $requirementsHash = Get-ContentHash -Path $requirementsPath
    $installedHash = if (Test-Path -LiteralPath $requirementsStamp -PathType Leaf) {
        (Get-Content -LiteralPath $requirementsStamp -Raw).Trim()
    }
    else {
        ""
    }
    $runtimeProbe = @(
        "import Crypto, fastapi, uvicorn, pymem, pefile, yara, psutil, aiofiles, multipart",
        "import PIL, pillow_heif, av, pysilk, zstandard, win32api, win32gui"
    ) -join "; "
    & $VenvPython -c $runtimeProbe 2>$null
    $runtimeProbeFailed = $LASTEXITCODE -ne 0
    $needsSynchronization = $runtimeProbeFailed -or $installedHash -ne $requirementsHash
    if ($needsSynchronization) {
        # A failed synchronization must never leave a success stamp that causes
        # the next run to skip its repair attempt.
        Remove-Item -LiteralPath $requirementsStamp -Force -ErrorAction SilentlyContinue
        Write-Host "Updating the Python package installer..."
        Invoke-Checked $VenvPython -m pip install --disable-pip-version-check `
            --upgrade "pip>=26.2,<27"
        Write-Host "Synchronizing Python runtime dependencies..."
        Invoke-Checked $VenvPython -m pip install --disable-pip-version-check `
            --only-binary=av,pillow-heif,pillow `
            -r $requirementsPath
        & $VenvPython -c $runtimeProbe 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Python dependencies were installed, but the runtime import check still failed."
        }
    }
    Invoke-Checked $VenvPython -m pip check
    if ($needsSynchronization) {
        [System.IO.File]::WriteAllText(
            $requirementsStamp,
            $requirementsHash + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
}

function Build-Frontend {
    if ($SkipFrontendBuild) {
        if (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot "dist\index.html") -PathType Leaf)) {
            throw "-SkipFrontendBuild requires an existing frontend/dist build."
        }
        return
    }

    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $nodeVersionOutput = & $node --version
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to query the installed Node.js version."
    }
    $nodeVersionText = ([string]$nodeVersionOutput).Trim().TrimStart("v")
    if ([Version]$nodeVersionText -lt [Version]"20.19.0") {
        throw "Node.js 20.19 or newer is required; detected $nodeVersionText."
    }
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    $lockPath = Join-Path $FrontendRoot "package-lock.json"
    $dependencyRoot = Join-Path $FrontendRoot "node_modules"
    $lockStamp = Join-Path $dependencyRoot ".wechat-analysis-package-lock.sha256"
    $lockHash = Get-ContentHash -Path $lockPath
    $installedHash = if (Test-Path -LiteralPath $lockStamp -PathType Leaf) {
        (Get-Content -LiteralPath $lockStamp -Raw).Trim()
    }
    else {
        ""
    }
    $needsInstall = (
        -not (Test-Path -LiteralPath $dependencyRoot -PathType Container) -or
        $installedHash -ne $lockHash
    )
    if (-not $needsInstall) {
        & $npm --prefix $FrontendRoot ls --depth=0 --silent *> $null
        $needsInstall = $LASTEXITCODE -ne 0
    }
    if ($needsInstall) {
        Write-Host "Synchronizing frontend dependencies with npm ci..."
        Invoke-Checked $npm --prefix $FrontendRoot ci
        [System.IO.File]::WriteAllText(
            $lockStamp,
            $lockHash + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
    }
    Write-Host "Building the browser frontend..."
    Invoke-Checked $npm --prefix $FrontendRoot run build
}

function Wait-ForServer {
    param([Parameter(Mandatory = $true)] $Process)

    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            throw "The backend exited during startup with code $($Process.ExitCode)."
        }
        try {
            $response = Invoke-WebRequest `
                -UseBasicParsing `
                -Uri "http://127.0.0.1:$Port/api/status" `
                -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 350
        }
    }
    throw "The backend did not become ready on port $Port within 45 seconds."
}

Push-Location $ProjectRoot
try {
    Write-Host "WechatAnalysisAssistant browser edition"
    Build-Frontend
    Initialize-PythonEnvironment
    # Keep an already working server alive until every prerequisite succeeds.
    Stop-PreviousProjectServer

    # The backend is a local helper process; keep its console hidden.
    $server = Start-Process `
        -FilePath $VenvPython `
        -ArgumentList @("-m", "backend.main", "--no-browser", "--port", "$Port") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -PassThru
    try {
        Wait-ForServer -Process $server
    }
    catch {
        if (-not $server.HasExited) {
            Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        }
        throw
    }

    $address = "http://127.0.0.1:$Port"
    Write-Host "Server ready: $address" -ForegroundColor Green
    if (-not $NoBrowser) {
        Start-Process $address
    }
}
finally {
    Pop-Location
}
