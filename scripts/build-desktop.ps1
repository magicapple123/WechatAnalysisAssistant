[CmdletBinding()]
param(
    [ValidateSet("beta", "release")]
    [string] $Channel = "beta",
    [switch] $SkipFrontend,
    [switch] $SkipBackend,
    [switch] $SkipElectron,
    [switch] $SkipSmokeTest,
    [switch] $IncludeWxKey
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$DesktopRoot = Join-Path $ProjectRoot "desktop"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Spec = Join-Path $ProjectRoot "packaging\wechat_backend.spec"
$BackendDistRoot = Join-Path $ProjectRoot "dist\backend"
$BackendWorkRoot = Join-Path $ProjectRoot "build\pyinstaller"
$SafetyCheck = Join-Path $PSScriptRoot "check-release.ps1"
$DesktopSmokeTest = Join-Path $PSScriptRoot "smoke-desktop.ps1"
$DesktopResourcesRoot = Join-Path $DesktopRoot "resources"
$DesktopOutputRoot = Join-Path $DesktopRoot "dist-electron"
$FrozenBackendRoot = Join-Path $BackendDistRoot "WechatAnalysisAssistantBackend"
$FrozenBackendExe = Join-Path $FrozenBackendRoot "WechatAnalysisAssistantBackend.exe"

function Invoke-NativeStep {
    param(
        [Parameter(Mandatory = $true)][string] $Label,
        [Parameter(Mandatory = $true)][scriptblock] $Action
    )
    Write-Host "`n==> $Label" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Sync-BuildDirectory {
    param(
        [Parameter(Mandatory = $true)][string] $Source,
        [Parameter(Mandatory = $true)][string] $Destination
    )
    $sourceFull = [System.IO.Path]::GetFullPath($Source)
    $destinationFull = [System.IO.Path]::GetFullPath($Destination)
    $allowedRoot = [System.IO.Path]::GetFullPath($DesktopResourcesRoot).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $destinationFull.StartsWith($allowedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace a staging directory outside desktop/resources: $destinationFull"
    }
    if (-not (Test-Path -LiteralPath $sourceFull -PathType Container)) {
        throw "Build output is missing: $sourceFull"
    }
    if (Test-Path -LiteralPath $destinationFull) {
        Remove-Item -LiteralPath $destinationFull -Recurse -Force
    }
    $destinationParent = Split-Path -Parent $destinationFull
    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    Copy-Item -LiteralPath $sourceFull -Destination $destinationFull -Recurse -Force
}

function Reset-DesktopOutputDirectory {
    $outputFull = [System.IO.Path]::GetFullPath($DesktopOutputRoot)
    $desktopPrefix = [System.IO.Path]::GetFullPath($DesktopRoot).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $outputFull.StartsWith($desktopPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean an Electron output directory outside desktop: $outputFull"
    }
    if ([System.IO.Path]::GetFileName($outputFull) -ne "dist-electron") {
        throw "Refusing to clean an unexpected Electron output directory: $outputFull"
    }
    if (Test-Path -LiteralPath $outputFull) {
        Remove-Item -LiteralPath $outputFull -Recurse -Force
    }
}

function Test-ReleaseUpdateUrl {
    param([Parameter(Mandatory = $true)][string] $Url)

    [Uri] $parsed = $null
    if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$parsed)) {
        return $false
    }
    $hostname = $parsed.DnsSafeHost.ToLowerInvariant()
    return (
        $parsed.Scheme -eq [Uri]::UriSchemeHttps -and
        [string]::IsNullOrEmpty($parsed.UserInfo) -and
        -not [string]::IsNullOrEmpty($hostname) -and
        $hostname -ne "example.invalid" -and
        -not $hostname.EndsWith(".example.invalid")
    )
}

function Test-ReleasePublisher {
    param([Parameter(Mandatory = $true)] $Publisher)

    $provider = [string]$Publisher.provider
    if ($provider -eq "github") {
        $owner = [string]$Publisher.owner
        $repository = [string]$Publisher.repo
        return (
            $owner.Length -le 39 -and
            $owner -match '^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$' -and
            -not $owner.Contains("--") -and
            $repository.Length -le 100 -and
            $repository -match '^[A-Za-z0-9_.-]+$' -and
            $repository -notin @(".", "..")
        )
    }
    if ($provider -eq "generic") {
        return (Test-ReleaseUpdateUrl -Url ([string]$Publisher.url))
    }
    return $false
}

function Get-SimpleYamlValue {
    param(
        [Parameter(Mandatory = $true)][string] $Content,
        [Parameter(Mandatory = $true)][string] $Key
    )

    $escapedKey = [regex]::Escape($Key)
    $match = [regex]::Match(
        $Content,
        "(?m)^\s*$escapedKey\s*:\s*(?<value>[^#\r\n]+?)\s*(?:#.*)?$"
    )
    if (-not $match.Success) {
        throw "Update metadata is missing the '$Key' field."
    }
    return $match.Groups["value"].Value.Trim().Trim("'", '"')
}

function Confirm-ChannelUpdateMetadata {
    param(
        [Parameter(Mandatory = $true)][string] $OutputRoot,
        [Parameter(Mandatory = $true)][string] $BuildChannel,
        [Parameter(Mandatory = $true)][string] $ExpectedVersion
    )

    $metadataName = if ($BuildChannel -eq "beta") { "beta.yml" } else { "latest.yml" }
    $metadataPath = Join-Path $OutputRoot $metadataName
    if ($BuildChannel -eq "beta" -and -not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
        # electron-builder intentionally emits latest.yml for the GitHub provider,
        # even for a SemVer prerelease. electron-updater can fall back to it after
        # a 404, but publishing beta.yml avoids that request and makes the release
        # channel artifact explicit.
        $latestMetadataPath = Join-Path $OutputRoot "latest.yml"
        if (-not (Test-Path -LiteralPath $latestMetadataPath -PathType Leaf)) {
            throw "Electron Builder produced neither beta.yml nor latest.yml."
        }
        Copy-Item -LiteralPath $latestMetadataPath -Destination $metadataPath
    }
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
        throw "Electron Builder did not produce the required $metadataName update metadata."
    }

    $content = [System.IO.File]::ReadAllText($metadataPath, [System.Text.Encoding]::UTF8)
    $metadataVersion = Get-SimpleYamlValue -Content $content -Key "version"
    $artifactName = Get-SimpleYamlValue -Content $content -Key "path"
    $metadataSize = Get-SimpleYamlValue -Content $content -Key "size"
    $metadataSha512 = Get-SimpleYamlValue -Content $content -Key "sha512"
    if ($metadataVersion -ne $ExpectedVersion) {
        throw "Update metadata version $metadataVersion does not match desktop version $ExpectedVersion."
    }
    if ([System.IO.Path]::GetFileName($artifactName) -ne $artifactName) {
        throw "Update metadata contains an unsafe artifact path: $artifactName"
    }
    $artifactPath = Join-Path $OutputRoot $artifactName
    if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf)) {
        throw "Update metadata references a missing installer: $artifactName"
    }
    $artifact = Get-Item -LiteralPath $artifactPath
    [long] $expectedSize = 0
    if (-not [long]::TryParse($metadataSize, [ref]$expectedSize) -or $expectedSize -ne $artifact.Length) {
        throw "Update metadata size does not match the installer: $artifactName"
    }

    $stream = [System.IO.File]::OpenRead($artifact.FullName)
    $sha512 = [System.Security.Cryptography.SHA512]::Create()
    try {
        $actualSha512 = [Convert]::ToBase64String($sha512.ComputeHash($stream))
    }
    finally {
        $sha512.Dispose()
        $stream.Dispose()
    }
    if ($metadataSha512 -ne $actualSha512) {
        throw "Update metadata SHA-512 does not match the installer: $artifactName"
    }
}

function Test-FrozenWxKeyIncluded {
    param([Parameter(Mandatory = $true)][string] $BackendRoot)
    return (Test-Path -LiteralPath (Join-Path $BackendRoot "_internal\wx_key") -PathType Container)
}

function Get-PyInstallerBuildPath {
    $basePrefixOutput = @(& $Python -c "import sys; print(sys.base_prefix)")
    if ($LASTEXITCODE -ne 0 -or $basePrefixOutput.Count -eq 0) {
        throw "Unable to determine the Python base installation directory."
    }
    $pythonBase = [System.IO.Path]::GetFullPath([string]$basePrefixOutput[-1])
    $systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    $paths = @(
        (Split-Path -Parent $Python),
        $pythonBase,
        (Join-Path $pythonBase "DLLs"),
        $systemDirectory,
        $env:SystemRoot,
        (Join-Path $systemDirectory "Wbem"),
        (Join-Path $systemDirectory "WindowsPowerShell\v1.0")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Container) }
    return (@($paths | Select-Object -Unique) -join [System.IO.Path]::PathSeparator)
}

function Normalize-FrozenNativeRuntime {
    param([Parameter(Mandatory = $true)][string] $BackendRoot)

    $backendFull = [System.IO.Path]::GetFullPath($BackendRoot)
    $allowedRoot = [System.IO.Path]::GetFullPath($BackendDistRoot).TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $backendFull.StartsWith($allowedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to normalize a native runtime outside the backend build directory: $backendFull"
    }
    $internal = Join-Path $backendFull "_internal"
    if (-not (Test-Path -LiteralPath $internal -PathType Container)) {
        throw "Frozen backend runtime directory is missing: $internal"
    }

    # PyInstaller resolves transitive DLLs through PATH. A JDK on the build
    # machine may carry an older private C++ runtime and silently pollute the
    # package. Use one coherent Microsoft runtime and let Windows provide UCRT.
    Get-ChildItem -LiteralPath $internal -File -Filter "api-ms-win-crt-*.dll" |
        Remove-Item -Force
    $localUcrt = Join-Path $internal "ucrtbase.dll"
    if (Test-Path -LiteralPath $localUcrt -PathType Leaf) {
        Remove-Item -LiteralPath $localUcrt -Force
    }

    $systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    $runtimeNames = @("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
    $runtimeVersions = @()
    foreach ($runtimeName in $runtimeNames) {
        $source = Join-Path $systemDirectory $runtimeName
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Microsoft Visual C++ runtime is missing: $source"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $internal $runtimeName) -Force
        $runtimeVersions += (Get-Item -LiteralPath $source).VersionInfo.FileVersion
    }
    $runtimeFamilies = @($runtimeVersions | ForEach-Object {
        if ($_ -match '^(\d+\.\d+)\.') { $Matches[1] } else { $_ }
    } | Select-Object -Unique)
    if ($runtimeFamilies.Count -ne 1) {
        throw "Microsoft Visual C++ runtime files are not from one compatible family: $($runtimeVersions -join ', ')"
    }
    Write-Host "Normalized frozen VC runtime: $($runtimeVersions -join ', ')" -ForegroundColor DarkGray
}

Push-Location $ProjectRoot
try {
    if (-not $SkipFrontend) {
        if (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot "node_modules") -PathType Container)) {
            throw "frontend/node_modules is missing. Run 'npm ci --prefix frontend' first."
        }
        Invoke-NativeStep "Build frontend" {
            npm run build --prefix $FrontendRoot
        }
    }

    if (-not $SkipBackend) {
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            throw ".venv is missing. Create it and install backend/requirements.txt first."
        }
        Invoke-NativeStep "Verify native/runtime packaging dependencies" {
            & $Python (Join-Path $PSScriptRoot "verify-packaging-runtime.py")
        }
        $wxKeyAvailable = $false
        if ($IncludeWxKey) {
            $previousErrorPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = "SilentlyContinue"
                & $Python -c "import wx_key" *> $null
                $wxKeyAvailable = $LASTEXITCODE -eq 0
            }
            finally {
                $ErrorActionPreference = $previousErrorPreference
            }
            if (-not $wxKeyAvailable) {
                throw "-IncludeWxKey was requested, but no compatible wx_key module is installed in .venv."
            }
            Write-Warning "Including the locally supplied wx_key enhancement. Confirm redistribution rights before sharing this build."
        }
        else {
            Write-Host "Building the public fallback edition without the unlicensed wx_key enhancement." -ForegroundColor DarkGray
        }
        $previousErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "SilentlyContinue"
            & $Python -c "import PyInstaller" *> $null
            $pyInstallerExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorPreference
        }
        if ($pyInstallerExitCode -ne 0) {
            throw "PyInstaller is missing. Install packaging/requirements-build.txt into .venv first."
        }
        $previousPath = $env:PATH
        $previousWxKeyBuildFlag = $env:WECHAT_ASSISTANT_INCLUDE_WX_KEY_BUILD
        try {
            $env:PATH = Get-PyInstallerBuildPath
            $env:WECHAT_ASSISTANT_INCLUDE_WX_KEY_BUILD = if ($wxKeyAvailable) { "1" } else { "0" }
            Invoke-NativeStep "Build PyInstaller backend sidecar (onedir)" {
                & $Python -m PyInstaller `
                    --noconfirm `
                    --clean `
                    --distpath $BackendDistRoot `
                    --workpath $BackendWorkRoot `
                    $Spec
            }
        }
        finally {
            $env:PATH = $previousPath
            $env:WECHAT_ASSISTANT_INCLUDE_WX_KEY_BUILD = $previousWxKeyBuildFlag
        }
        Normalize-FrozenNativeRuntime -BackendRoot $FrozenBackendRoot
        $frozenIncludesWxKey = Test-FrozenWxKeyIncluded -BackendRoot $FrozenBackendRoot
        if ($frozenIncludesWxKey -ne $wxKeyAvailable) {
            throw "Frozen wx_key contents do not match the requested public/opt-in build mode."
        }
        if ($wxKeyAvailable) {
            Invoke-NativeStep "Probe optional frozen Hook key component" {
                & $FrozenBackendExe --probe-native-module wx_key
            }
        }
        Invoke-NativeStep "Scan backend sidecar for private data" {
            & $SafetyCheck $FrozenBackendRoot
        }
    }

    if (-not $SkipElectron) {
        if (-not (Test-Path -LiteralPath (Join-Path $DesktopRoot "package.json") -PathType Leaf)) {
            throw "desktop/package.json is missing. The Electron shell must be present before packaging."
        }
        if (-not (Test-Path -LiteralPath (Join-Path $DesktopRoot "node_modules") -PathType Container)) {
            throw "desktop/node_modules is missing. Run 'npm ci --prefix desktop' first."
        }
        if ($SkipBackend) {
            $frozenIncludesWxKey = Test-FrozenWxKeyIncluded -BackendRoot $FrozenBackendRoot
            if ($frozenIncludesWxKey -ne [bool]$IncludeWxKey) {
                throw "The prebuilt sidecar's wx_key contents do not match -IncludeWxKey. Rebuild the backend with the intended mode."
            }
        }
        Sync-BuildDirectory `
            -Source $FrozenBackendRoot `
            -Destination (Join-Path $DesktopResourcesRoot "backend\WechatAnalysisAssistantBackend")
        $desktopPackage = Get-Content -LiteralPath (Join-Path $DesktopRoot "package.json") -Raw | ConvertFrom-Json
        $scripts = $desktopPackage.scripts
        $desktopVersion = [string]$desktopPackage.version
        if ($Channel -eq "beta" -and $desktopVersion -notmatch '-(?:alpha|beta|rc)(?:\.|$)') {
            throw "Beta channel requires a prerelease version such as 1.2.0-beta.1; found $desktopVersion."
        }
        if ($Channel -eq "release" -and $desktopVersion.Contains("-")) {
            throw "Release channel requires a stable SemVer without a prerelease suffix; found $desktopVersion."
        }
        $publishers = @($desktopPackage.build.publish)
        $invalidPublishers = @(
            $publishers | Where-Object { -not (Test-ReleasePublisher -Publisher $_) }
        )
        if (
            $publishers.Count -eq 0 -or
            $invalidPublishers.Count -gt 0
        ) {
            throw "$Channel channel requires a valid HTTPS or GitHub update publisher in desktop/package.json."
        }
        Invoke-NativeStep "Run Electron unit tests" {
            npm test --prefix $DesktopRoot
        }
        Invoke-NativeStep "Verify Electron packaging configuration" {
            npm run check --prefix $DesktopRoot
        }
        $electronScript = if ($scripts.PSObject.Properties.Name -contains "dist:package") {
            "dist:package"
        }
        elseif ($Channel -eq "release" -and $scripts.PSObject.Properties.Name -contains "dist:release") {
            "dist:release"
        }
        elseif ($Channel -eq "beta" -and $scripts.PSObject.Properties.Name -contains "dist:beta") {
            "dist:beta"
        }
        elseif ($scripts.PSObject.Properties.Name -contains "dist") {
            "dist"
        }
        else {
            throw "desktop/package.json must define dist, dist:beta, or dist:release."
        }
        Reset-DesktopOutputDirectory
        Invoke-NativeStep "Build Electron/NSIS package ($Channel)" {
            npm run $electronScript --prefix $DesktopRoot
        }
        Invoke-NativeStep "Prepare and verify $Channel update metadata" {
            Confirm-ChannelUpdateMetadata `
                -OutputRoot $DesktopOutputRoot `
                -BuildChannel $Channel `
                -ExpectedVersion $desktopVersion
        }

        $unpackedCandidate = Join-Path $DesktopOutputRoot "win-unpacked"
        if (-not (Test-Path -LiteralPath $unpackedCandidate -PathType Container)) {
            throw "Electron build completed but no win-unpacked directory was found for the safety scan."
        }
        $requiredPackagedFiles = @(
            (Join-Path $unpackedCandidate "resources\app.asar"),
            (Join-Path $unpackedCandidate "resources\app-update.yml"),
            (Join-Path $unpackedCandidate "resources\frontend\index.html"),
            (Join-Path $unpackedCandidate "resources\backend\WechatAnalysisAssistantBackend\WechatAnalysisAssistantBackend.exe")
        )
        foreach ($requiredFile in $requiredPackagedFiles) {
            if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
                throw "Packaged application is incomplete: $requiredFile"
            }
        }
        Invoke-NativeStep "Scan Electron staging directory for private data" {
            & $SafetyCheck $unpackedCandidate
        }
        $applicationExecutables = @(
            Get-ChildItem -LiteralPath $unpackedCandidate -Filter "*.exe" -File |
                Where-Object { $_.Name -notin @("elevate.exe", "uninstall.exe") }
        )
        if ($applicationExecutables.Count -ne 1) {
            throw "Expected exactly one packaged application executable in $unpackedCandidate; found $($applicationExecutables.Count)."
        }
        $applicationExecutable = $applicationExecutables[0].FullName
        Invoke-NativeStep "Verify packaged Electron security fuses" {
            npm run verify:fuses --prefix $DesktopRoot -- $applicationExecutable
        }
        if (-not $SkipSmokeTest) {
            Invoke-NativeStep "Smoke test packaged Electron application" {
                & $DesktopSmokeTest -ApplicationPath $applicationExecutable
            }
        }
    }

    Write-Host "`nDesktop build completed ($Channel)." -ForegroundColor Green
}
finally {
    Pop-Location
}
