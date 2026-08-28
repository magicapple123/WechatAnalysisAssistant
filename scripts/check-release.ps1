[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]] $Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ForbiddenSegments = @(
    ".venv",
    "venv",
    "node_modules",
    "wechat-decrypt-main",
    ".diagnostic_localappdata",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "cache",
    "caches"
)
$ForbiddenNames = @(
    "settings.json",
    "wechat_key.txt",
    "wechat_keys.txt",
    "wechat_keys.json",
    "image_descriptions.sqlite3",
    "voice_transcriptions.sqlite3"
)
$ForbiddenPatterns = @(
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite3",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "*.dmp",
    "*.log",
    "*.pfx",
    "*.p12",
    "settings.json.*",
    "wechat_key*.txt*",
    "wechat_keys*.json*",
    ".env",
    ".env.*",
    ".diag*"
)
$ForbiddenAsarPackages = @(
    "electron",
    "electron-builder",
    "app-builder-bin",
    "builder-util",
    "dmg-builder",
    "@electron/rebuild",
    "@electron/packager"
)

$Violations = [System.Collections.Generic.List[string]]::new()

function Test-ReleaseEntry {
    param(
        [Parameter(Mandatory = $true)][string] $Entry,
        [Parameter(Mandatory = $true)][string] $Container
    )

    $normalized = $Entry.TrimStart([char[]]@("/", "\")) -replace "\\", "/"
    if ([string]::IsNullOrWhiteSpace($normalized)) {
        return
    }

    $segments = @($normalized.Split("/", [System.StringSplitOptions]::RemoveEmptyEntries))
    $isAsarEntry = $Container.EndsWith(".asar", [System.StringComparison]::OrdinalIgnoreCase)
    for ($index = 0; $index -lt $segments.Count; $index++) {
        $segment = $segments[$index]
        $lowerSegment = $segment.ToLowerInvariant()
        if ($lowerSegment -eq "node_modules" -and $isAsarEntry) {
            # Electron needs a small set of production packages such as
            # electron-updater. Permit those inside app.asar, while rejecting
            # build-time Electron tooling and any physical node_modules tree.
            if ($index + 1 -lt $segments.Count) {
                $packageName = $segments[$index + 1].ToLowerInvariant()
                if ($packageName.StartsWith("@") -and $index + 2 -lt $segments.Count) {
                    $packageName += "/" + $segments[$index + 2].ToLowerInvariant()
                }
                if ($ForbiddenAsarPackages -contains $packageName) {
                    $Violations.Add("$Container :: $normalized (development package '$packageName')")
                    return
                }
            }
            continue
        }
        if ($ForbiddenSegments -contains $lowerSegment) {
            $Violations.Add("$Container :: $normalized (forbidden directory '$segment')")
            return
        }
    }

    $fileName = $segments[-1]
    if ($ForbiddenNames -contains $fileName.ToLowerInvariant()) {
        $Violations.Add("$Container :: $normalized (sensitive file name)")
        return
    }
    foreach ($pattern in $ForbiddenPatterns) {
        if ($fileName -like $pattern) {
            $Violations.Add("$Container :: $normalized (matches '$pattern')")
            return
        }
    }
}

function Find-AsarCommand {
    $candidates = @(
        (Join-Path $ProjectRoot "desktop\node_modules\.bin\asar.cmd"),
        (Join-Path $ProjectRoot "node_modules\.bin\asar.cmd")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

function Inspect-Asar {
    param([Parameter(Mandatory = $true)][string] $AsarPath)

    $asarCommand = Find-AsarCommand
    if (-not $asarCommand) {
        $Violations.Add("$AsarPath (cannot inspect app.asar: local asar CLI is missing)")
        return
    }
    $entries = & $asarCommand list $AsarPath 2>&1
    if ($LASTEXITCODE -ne 0) {
        $Violations.Add("$AsarPath (asar listing failed)")
        return
    }
    foreach ($entry in $entries) {
        Test-ReleaseEntry -Entry ([string]$entry) -Container $AsarPath
    }
}

function Inspect-Zip {
    param([Parameter(Mandatory = $true)][string] $ZipPath)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        foreach ($entry in $archive.Entries) {
            Test-ReleaseEntry -Entry $entry.FullName -Container $ZipPath
        }
    }
    finally {
        $archive.Dispose()
    }
}

foreach ($inputPath in $Path) {
    $resolved = Resolve-Path -LiteralPath $inputPath -ErrorAction Stop
    $item = Get-Item -LiteralPath $resolved.Path -Force
    if ($item.PSIsContainer) {
        $findingsBeforeRootCheck = $Violations.Count
        $rootContainer = if ($item.Parent) { $item.Parent.FullName } else { $item.FullName }
        Test-ReleaseEntry -Entry $item.Name -Container $rootContainer
        if ($Violations.Count -gt $findingsBeforeRootCheck) {
            continue
        }
        foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force -Recurse) {
            $rootPrefix = $item.FullName.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
            $relative = $child.FullName.Substring($rootPrefix.Length)
            Test-ReleaseEntry -Entry $relative -Container $item.FullName
            if (-not $child.PSIsContainer -and $child.Extension -ieq ".asar") {
                Inspect-Asar -AsarPath $child.FullName
            }
            elseif (-not $child.PSIsContainer -and $child.Extension -ieq ".zip") {
                Inspect-Zip -ZipPath $child.FullName
            }
        }
    }
    else {
        Test-ReleaseEntry -Entry $item.Name -Container $item.DirectoryName
        if ($item.Extension -ieq ".asar") {
            Inspect-Asar -AsarPath $item.FullName
        }
        elseif ($item.Extension -ieq ".zip") {
            Inspect-Zip -ZipPath $item.FullName
        }
    }
}

if ($Violations.Count -gt 0) {
    Write-Host "Release safety check FAILED ($($Violations.Count) finding(s)):" -ForegroundColor Red
    foreach ($violation in $Violations) {
        Write-Host "  - $violation" -ForegroundColor Red
    }
    exit 1
}

Write-Host "Release safety check passed: no private runtime data or source caches found." -ForegroundColor Green
exit 0
