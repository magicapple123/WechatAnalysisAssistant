[CmdletBinding()]
param(
    [string] $ApplicationPath = "",
    [ValidateRange(10, 300)]
    [int] $TimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$candidate = if ([string]::IsNullOrWhiteSpace($ApplicationPath)) {
    $unpackedRoot = Join-Path $ProjectRoot "desktop\dist-electron\win-unpacked"
    $executables = @(
        Get-ChildItem -LiteralPath $unpackedRoot -Filter "*.exe" -File -ErrorAction Stop |
            Where-Object { $_.Name -notin @("elevate.exe", "uninstall.exe") }
    )
    if ($executables.Count -ne 1) {
        throw "Expected exactly one packaged application executable in $unpackedRoot; found $($executables.Count)."
    }
    $executables[0].FullName
}
elseif ([System.IO.Path]::IsPathRooted($ApplicationPath)) {
    $ApplicationPath
}
else {
    Join-Path $ProjectRoot $ApplicationPath
}
$executable = [System.IO.Path]::GetFullPath($candidate)
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Packaged Electron executable is missing: $executable"
}
$expectedBackendExecutable = [System.IO.Path]::GetFullPath((Join-Path (
    Split-Path -Parent $executable
) "resources\backend\WechatAnalysisAssistantBackend\WechatAnalysisAssistantBackend.exe"))
if (-not (Test-Path -LiteralPath $expectedBackendExecutable -PathType Leaf)) {
    throw "Packaged backend sidecar is missing: $expectedBackendExecutable"
}

$temporaryBase = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::GetTempPath()
).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
$smokeRoot = [System.IO.Path]::GetFullPath((
    Join-Path $temporaryBase ("WechatAnalysisAssistant-smoke-" + [guid]::NewGuid().ToString("N"))
))
if (-not $smokeRoot.StartsWith($temporaryBase, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create a smoke-test directory outside the system temporary directory."
}
New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null

$stdoutPath = Join-Path $smokeRoot "electron.stdout.log"
$stderrPath = Join-Path $smokeRoot "electron.stderr.log"
$previousDataDir = $env:WECHAT_ASSISTANT_DATA_DIR
$previousDesktopDataDir = $env:WECHAT_ASSISTANT_DESKTOP_USER_DATA_DIR
$previousSmokeFlag = $env:WECHAT_ASSISTANT_SMOKE_TEST
$previousSmokeTimeout = $env:WECHAT_ASSISTANT_SMOKE_TIMEOUT_MS
$previousElectronLogging = $env:ELECTRON_ENABLE_LOGGING
$process = $null
$backendProcessId = $null

function Get-SmokeDiagnostics {
    $stdout = if (Test-Path -LiteralPath $stdoutPath) {
        Get-Content -LiteralPath $stdoutPath -Raw -Encoding utf8
    }
    else { "" }
    $stderr = if (Test-Path -LiteralPath $stderrPath) {
        Get-Content -LiteralPath $stderrPath -Raw -Encoding utf8
    }
    else { "" }
    $backendLog = Join-Path $smokeRoot "desktop-data\logs\backend.log"
    $backend = if (Test-Path -LiteralPath $backendLog) {
        Get-Content -LiteralPath $backendLog -Raw -Encoding utf8
    }
    else { "" }
    $combined = "STDOUT:`n$stdout`nSTDERR:`n$stderr`nBACKEND:`n$backend"
    return $combined `
        -replace '(?i)\bwxid_[a-z0-9_-]+\b', '[redacted-wechat-id]' `
        -replace '(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])', '[redacted-64-hex-value]'
}

function Stop-SmokeProcessTree {
    param([System.Diagnostics.Process] $TargetProcess)

    if (-not $TargetProcess -or $TargetProcess.HasExited) {
        return
    }
    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    try {
        & $taskkill /PID ([string]$TargetProcess.Id) /T /F *> $null
        $TargetProcess.WaitForExit(5000) | Out-Null
    }
    catch {
        # Fall through to the direct-process fallback below.
    }
    if (-not $TargetProcess.HasExited) {
        Stop-Process -Id $TargetProcess.Id -Force -ErrorAction SilentlyContinue
    }
}

function Get-SmokeBackendMarker {
    param([Parameter(Mandatory = $true)][string] $StandardOutput)

    $matches = [regex]::Matches(
        $StandardOutput,
        '(?m)^[ \t]*WECHAT_ASSISTANT_SMOKE_BACKEND=(?<payload>[A-Za-z0-9+/]+={0,2})\r?$'
    )
    if ($matches.Count -ne 1) {
        throw "Expected exactly one packaged backend identity marker; found $($matches.Count)."
    }
    try {
        $bytes = [Convert]::FromBase64String($matches[0].Groups["payload"].Value)
        $json = [System.Text.Encoding]::UTF8.GetString($bytes)
        $marker = $json | ConvertFrom-Json
    }
    catch {
        throw "Packaged backend identity marker was invalid: $($_.Exception.Message)"
    }

    [int] $markerPid = 0
    if (-not [int]::TryParse([string]$marker.pid, [ref]$markerPid) -or $markerPid -le 0) {
        throw "Packaged backend identity marker contained an invalid process ID."
    }
    $markerExecutable = [string]$marker.executable
    if ([string]::IsNullOrWhiteSpace($markerExecutable) -or -not [System.IO.Path]::IsPathRooted($markerExecutable)) {
        throw "Packaged backend identity marker contained an invalid executable path."
    }
    return [pscustomobject]@{
        ProcessId = $markerPid
        Executable = [System.IO.Path]::GetFullPath($markerExecutable)
    }
}

function Test-ProcessExists {
    param([Parameter(Mandatory = $true)][int] $ProcessId)

    try {
        $candidateProcess = [System.Diagnostics.Process]::GetProcessById($ProcessId)
        try {
            return -not $candidateProcess.HasExited
        }
        finally {
            $candidateProcess.Dispose()
        }
    }
    catch [System.ArgumentException] {
        return $false
    }
}

function Get-ProcessExecutablePath {
    param([Parameter(Mandatory = $true)][int] $ProcessId)

    try {
        $candidateProcess = [System.Diagnostics.Process]::GetProcessById($ProcessId)
        try {
            if ($candidateProcess.HasExited) {
                return ""
            }
            return [System.IO.Path]::GetFullPath($candidateProcess.MainModule.FileName)
        }
        finally {
            $candidateProcess.Dispose()
        }
    }
    catch [System.ArgumentException] {
        return ""
    }
}

function Stop-VerifiedBackendProcess {
    param(
        [Parameter(Mandatory = $true)][int] $ProcessId,
        [Parameter(Mandatory = $true)][string] $ExpectedExecutable
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ((Test-ProcessExists -ProcessId $ProcessId) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
    }
    if (-not (Test-ProcessExists -ProcessId $ProcessId)) {
        return
    }

    # Never terminate a process solely by PID: the PID may have been reused.
    # Resolve the live image and require the exact packaged sidecar path first.
    $actualExecutable = Get-ProcessExecutablePath -ProcessId $ProcessId
    if (-not $actualExecutable.Equals($ExpectedExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to terminate unexpected process $ProcessId ($actualExecutable)."
    }
    $backendProcess = [System.Diagnostics.Process]::GetProcessById($ProcessId)
    try {
        Stop-SmokeProcessTree -TargetProcess $backendProcess
    }
    finally {
        $backendProcess.Dispose()
    }
    if (Test-ProcessExists -ProcessId $ProcessId) {
        throw "Packaged backend process $ProcessId remained alive after smoke-test cleanup."
    }
}

function Assert-SmokeApiAccessLog {
    param([Parameter(Mandatory = $true)][string] $BackendLog)

    $required = [ordered]@{
        "GET /api/desktop/health" = 0
        "POST /api/desktop/self-test" = 0
        "POST /api/desktop/prepare-exit" = 0
    }
    $requests = [regex]::Matches(
        $BackendLog,
        '"(?<method>GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) (?<target>/api(?:[^ ]*)) HTTP/[0-9.]+"'
    )
    if ($requests.Count -eq 0) {
        throw "Packaged backend access log contained no auditable API requests."
    }
    foreach ($request in $requests) {
        $entry = "$($request.Groups['method'].Value) $($request.Groups['target'].Value)"
        if (-not $required.Contains($entry)) {
            throw "Packaged Electron smoke test made an unexpected API request: $entry"
        }
        $required[$entry] += 1
    }
    foreach ($entry in $required.GetEnumerator()) {
        if ($entry.Value -eq 0) {
            throw "Packaged Electron smoke test did not exercise required API request: $($entry.Key)"
        }
    }
}

try {
    $env:WECHAT_ASSISTANT_DATA_DIR = Join-Path $smokeRoot "backend-data"
    $env:WECHAT_ASSISTANT_DESKTOP_USER_DATA_DIR = Join-Path $smokeRoot "desktop-data"
    $env:WECHAT_ASSISTANT_SMOKE_TEST = "1"
    $env:WECHAT_ASSISTANT_SMOKE_TIMEOUT_MS = [string]($TimeoutSeconds * 1000)
    $env:ELECTRON_ENABLE_LOGGING = "1"

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $executable
    $startInfo.WorkingDirectory = Split-Path -Parent $executable
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $startInfo.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)

    # Start the process through the same Process instance that is later
    # inspected. Start-Process can return a reattached Process object on
    # Windows PowerShell 5.1, for which ExitCode is unavailable after exit.
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Packaged Electron smoke test process could not be started."
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $externalTimeoutMs = ($TimeoutSeconds + 15) * 1000
    $timedOut = -not $process.WaitForExit($externalTimeoutMs)
    if ($timedOut) {
        Stop-SmokeProcessTree -TargetProcess $process
    }
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    [System.IO.File]::WriteAllText($stdoutPath, $stdout, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText($stderrPath, $stderr, [System.Text.UTF8Encoding]::new($false))

    $backendMarker = Get-SmokeBackendMarker -StandardOutput $stdout
    $backendProcessId = $backendMarker.ProcessId
    if (-not $backendMarker.Executable.Equals(
        $expectedBackendExecutable,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Packaged application reported an unexpected backend executable: $($backendMarker.Executable)"
    }

    if ($timedOut) {
        throw "Packaged Electron smoke test exceeded the external timeout of $($TimeoutSeconds + 15) seconds.`n$(Get-SmokeDiagnostics)"
    }
    if ($process.ExitCode -ne 0) {
        throw "Packaged Electron smoke test failed with exit code $($process.ExitCode).`n$(Get-SmokeDiagnostics)"
    }

    $backendLog = Join-Path $smokeRoot "desktop-data\logs\backend.log"
    $backendOutput = if (Test-Path -LiteralPath $backendLog) {
        Get-Content -LiteralPath $backendLog -Raw -Encoding utf8
    }
    else { "" }
    $allOutput = "$stdout`n$stderr`n$backendOutput"
    $forbiddenLiveDataMarkers = @(
        "wxid_",
        "xwechat_files",
        "Weixin.exe",
        "WeChat.exe",
        "WeChat Files",
        "Active WeChat account:",
        "Detecting local WeChat data",
        "/api/auto-detect",
        "MicroMsg.db",
        "MsgAttach"
    )
    foreach ($marker in $forbiddenLiveDataMarkers) {
        if ($allOutput.IndexOf($marker, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Packaged Electron smoke test accessed live WeChat data (marker: $marker).`n$(Get-SmokeDiagnostics)"
        }
    }
    Assert-SmokeApiAccessLog -BackendLog $backendOutput

    Write-Host "Packaged Electron smoke test passed." -ForegroundColor Green
}
finally {
    $cleanupError = $null
    try {
        Stop-SmokeProcessTree -TargetProcess $process
        if ($backendProcessId) {
            # The Electron root may already have exited while a detached or
            # slow sidecar remains. Always audit the recorded backend PID.
            Stop-VerifiedBackendProcess `
                -ProcessId $backendProcessId `
                -ExpectedExecutable $expectedBackendExecutable
        }
    }
    catch {
        $cleanupError = $_
    }
    finally {
        if ($process) {
            $process.Dispose()
        }
    }
    $env:WECHAT_ASSISTANT_DATA_DIR = $previousDataDir
    $env:WECHAT_ASSISTANT_DESKTOP_USER_DATA_DIR = $previousDesktopDataDir
    $env:WECHAT_ASSISTANT_SMOKE_TEST = $previousSmokeFlag
    $env:WECHAT_ASSISTANT_SMOKE_TIMEOUT_MS = $previousSmokeTimeout
    $env:ELECTRON_ENABLE_LOGGING = $previousElectronLogging

    $verifiedSmokeRoot = [System.IO.Path]::GetFullPath($smokeRoot)
    if ($verifiedSmokeRoot.StartsWith($temporaryBase, [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $verifiedSmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($cleanupError) {
        throw $cleanupError
    }
}

exit 0
