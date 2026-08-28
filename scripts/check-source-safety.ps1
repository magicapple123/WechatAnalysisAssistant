<#
.SYNOPSIS
Reject sensitive or generated files before source code is published.

.DESCRIPTION
The current Git-tracked source is checked for forbidden runtime data, oversized
files, and a small set of high-confidence credential signatures. Reachable
history is also checked for forbidden paths and the same signatures. Matches
report a category and path but never print the matched credential value.

This is a focused publication guard, not an exhaustive secret scanner. Keep
provider-side secret scanning enabled and rotate any credential that may have
been exposed.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ForbiddenNames = @(
    ".env",
    "settings.json",
    "wechat_key.txt",
    "wechat_keys.txt",
    "wechat_keys.json",
    "image_descriptions.sqlite3",
    "voice_transcriptions.sqlite3"
)
$ForbiddenExtensions = @(
    ".db", ".sqlite", ".sqlite3", ".dmp", ".log", ".pfx", ".p12", ".key"
)
$ForbiddenSegments = @(
    ".venv", "venv", "node_modules", "build", "dist",
    ".diagnostic_localappdata", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache"
)
$SecretSignatures = [ordered]@{
    "private key" = '-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
    "GitHub token" = '(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'
    "AWS access key" = 'AKIA[0-9A-Z]{16}'
    "Google API key" = 'AIza[0-9A-Za-z_-]{35}'
    "Anthropic API key" = 'sk-ant-[A-Za-z0-9_-]{20,}'
}
$Violations = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$GitCommand = (Get-Command git.exe -ErrorAction Stop).Source
$GitExecutable = $GitCommand
# Git for Windows exposes a small cmd/git.exe launcher. That launcher starts a
# second git.exe process which can keep redirected standard handles open after
# the real command exits, so direct binary/stream reads would wait forever.
# Prefer the adjacent native executable whenever the launcher layout is used.
$gitRoot = Split-Path -Parent (Split-Path -Parent $GitCommand)
$nativeGitCandidate = Join-Path $gitRoot "mingw64\bin\git.exe"
if (Test-Path -LiteralPath $nativeGitCandidate -PathType Leaf) {
    $GitExecutable = $nativeGitCandidate
}

function Get-PathViolation {
    param([Parameter(Mandatory = $true)][string] $RelativePath)

    $normalized = $RelativePath -replace "\\", "/"
    $segments = @($normalized.Split("/", [System.StringSplitOptions]::RemoveEmptyEntries))
    if ($segments.Count -eq 0) {
        return $null
    }

    $fileName = $segments[-1]
    $lowerName = $fileName.ToLowerInvariant()
    $isPrivateEnvironmentFile = (
        $lowerName -eq ".env" -or
        ($lowerName.StartsWith(".env.") -and $lowerName -ne ".env.example")
    )
    if ($ForbiddenNames -contains $lowerName -or $isPrivateEnvironmentFile) {
        return "sensitive file name"
    }
    if ($lowerName -match '^wechat_key.*\.(txt|json)(?:\..*)?$') {
        return "sensitive key file"
    }
    if ($segments | Where-Object { $ForbiddenSegments -contains $_.ToLowerInvariant() }) {
        return "generated/private directory"
    }

    $extension = [System.IO.Path]::GetExtension($lowerName)
    if ($ForbiddenExtensions -contains $extension -or $lowerName -match '\.(db|sqlite|sqlite3)-(wal|shm)$') {
        return "runtime data file"
    }
    return $null
}

function Test-BytesForSignature {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]] $Bytes,
        [Parameter(Mandatory = $true)][string] $Pattern
    )

    # ISO-8859-1 preserves every ASCII byte exactly, which covers UTF-8 and
    # legacy single-byte source encodings. If NUL bytes are present, also test
    # both UTF-16 byte orders so Windows PowerShell-created files are covered.
    $views = [System.Collections.Generic.List[string]]::new()
    $views.Add([System.Text.Encoding]::GetEncoding(28591).GetString($Bytes))
    if ($Bytes -contains 0) {
        $views.Add([System.Text.Encoding]::Unicode.GetString($Bytes))
        $views.Add([System.Text.Encoding]::BigEndianUnicode.GetString($Bytes))
    }
    foreach ($view in $views) {
        if ([regex]::IsMatch($view, $Pattern)) {
            return $true
        }
    }
    return $false
}

function Read-GitBlobBytes {
    param([Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]+$')][string] $ObjectId)

    # Use redirected binary stdout instead of PowerShell's text redirection,
    # which would corrupt NUL-containing UTF-16 and other blob contents.
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $GitExecutable
    $startInfo.Arguments = "cat-file blob $ObjectId"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $buffer = [System.IO.MemoryStream]::new()
    try {
        if (-not $process.Start()) {
            throw "Unable to start Git blob reader."
        }
        $process.StandardOutput.BaseStream.CopyTo($buffer)
        $errorText = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            # Deliberately do not echo Git stderr because content-related errors
            # are not expected and publication checks must remain redacted.
            [void]$errorText
            throw "Unable to read a reachable Git blob."
        }
        Write-Output -NoEnumerate ([byte[]]$buffer.ToArray())
    }
    finally {
        $buffer.Dispose()
        $process.Dispose()
    }
}

function Get-GitObjectInformation {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [string[]] $ObjectIds
    )

    if ($ObjectIds.Count -eq 0) {
        return
    }

    # Do not pipe a PowerShell string array into Git here. Windows PowerShell
    # and newer Git releases have disagreed about the native-pipeline encoding
    # in hosted runners, which can prepend a BOM to the first object id. Feed
    # the ASCII ids through redirected stdin so --batch-check is deterministic.
    # A redirected Windows pipe can be only a few KiB. Keep each request below
    # that bound so Git cannot block on stdout while PowerShell is still
    # writing stdin (a classic two-pipe deadlock).
    $batchSize = 32
    for ($offset = 0; $offset -lt $ObjectIds.Count; $offset += $batchSize) {
        $lastIndex = [Math]::Min(
            $offset + $batchSize - 1,
            $ObjectIds.Count - 1
        )
        $batchIds = @($ObjectIds[$offset..$lastIndex])
        $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $GitExecutable
        $startInfo.Arguments = "cat-file --batch-check"
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardInput = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = [System.Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        try {
            if (-not $process.Start()) {
                throw "Unable to start Git object metadata reader."
            }
            foreach ($objectId in $batchIds) {
                if ($objectId -notmatch '^[0-9a-f]+$') {
                    throw "Git returned an invalid object identifier."
                }
            }
            # Process.StandardInput inherits the host console encoding under
            # Windows PowerShell 5.1. On GitHub runners its StreamWriter may
            # emit a UTF-8 BOM before the first write. A disposable first query
            # absorbs that preamble; all security-relevant full object ids then
            # remain ASCII and unambiguous.
            $inputLines = @("HEAD") + $batchIds
            $inputText = ($inputLines -join "`n") + "`n"
            $inputBytes = [System.Text.Encoding]::ASCII.GetBytes($inputText)
            $inputStream = $process.StandardInput.BaseStream
            $inputStream.Write($inputBytes, 0, $inputBytes.Length)
            $inputStream.Flush()
            $inputStream.Close()
            $standardOutput = $process.StandardOutput.ReadToEnd()
            $standardError = $process.StandardError.ReadToEnd()
            $process.WaitForExit()
            if ($process.ExitCode -ne 0) {
                # Deliberately keep Git stderr private: publication checks
                # should never echo content-derived diagnostics or credentials.
                [void]$standardError
                throw "Unable to inspect reachable Git object types."
            }

            $rawLines = @(
                $standardOutput -split '\r?\n' |
                    Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
            )
            if ($rawLines.Count -ne $batchIds.Count + 1) {
                throw "Git returned incomplete object metadata."
            }
            $lines = @($rawLines | Select-Object -Skip 1)
            for ($index = 0; $index -lt $lines.Count; $index++) {
                if (
                    $lines[$index] -notmatch
                        '^(?<id>[0-9a-f]+) (?<type>\S+) (?<size>\d+)$'
                ) {
                    throw "Git returned unexpected object metadata."
                }
                Write-Output (
                    "$($batchIds[$index]) $($Matches.type) $($Matches.size)"
                )
            }
        }
        finally {
            $process.Dispose()
        }
    }
}

Push-Location $ProjectRoot
try {
    $trackedFiles = @(& git ls-files)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to list Git-tracked files."
    }

    foreach ($relativePath in $trackedFiles) {
        $normalized = $relativePath -replace "\\", "/"
        $pathViolation = Get-PathViolation -RelativePath $normalized
        if ($pathViolation) {
            [void]$Violations.Add("${pathViolation}: $normalized")
            continue
        }

        $fullPath = Join-Path $ProjectRoot ($normalized -replace "/", "\")
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            continue
        }
        $file = Get-Item -LiteralPath $fullPath
        if ($file.Length -gt 25MB) {
            [void]$Violations.Add("file exceeds 25 MiB: $normalized")
            continue
        }

        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        foreach ($signature in $SecretSignatures.GetEnumerator()) {
            if (Test-BytesForSignature -Bytes $bytes -Pattern $signature.Value) {
                [void]$Violations.Add("$($signature.Key) signature: $normalized")
            }
        }
    }

    # Object paths reveal files deleted in later commits without exposing their
    # contents. Only history reachable from HEAD is relevant to the ref being
    # published; intentionally private local backup refs are not inspected.
    $reachableObjects = @(& git rev-list --objects HEAD)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to enumerate Git history reachable from HEAD."
    }
    $historyRecords = [System.Collections.Generic.List[object]]::new()
    foreach ($objectLine in $reachableObjects) {
        if ($objectLine -notmatch '^(?<id>[0-9a-f]+) (?<path>.+)$') {
            continue
        }
        $historyRecords.Add([pscustomobject]@{
            ObjectId = $Matches.id
            Path = ($Matches.path -replace "\\", "/")
        })
    }

    $objectIds = @(
        $historyRecords |
            Select-Object -ExpandProperty ObjectId -Unique
    )
    $batchInformation = @(Get-GitObjectInformation -ObjectIds $objectIds)
    $objectInformation = @{}
    foreach ($informationLine in $batchInformation) {
        if ($informationLine -notmatch '^(?<id>[0-9a-f]+) (?<type>\S+) (?<size>\d+)$') {
            throw "Git returned unexpected object metadata."
        }
        $objectInformation[$Matches.id] = [pscustomobject]@{
            Type = $Matches.type
            Size = [long]$Matches.size
        }
    }

    $cleanBlobPaths = @{}
    foreach ($record in $historyRecords) {
        $information = $objectInformation[$record.ObjectId]
        if (-not $information -or $information.Type -ne "blob") {
            continue
        }
        $historicalPath = $record.Path
        $pathViolation = Get-PathViolation -RelativePath $historicalPath
        if ($pathViolation) {
            [void]$Violations.Add("historical ${pathViolation}: $historicalPath")
            continue
        }
        if (-not $cleanBlobPaths.ContainsKey($record.ObjectId)) {
            $cleanBlobPaths[$record.ObjectId] = [System.Collections.Generic.HashSet[string]]::new(
                [System.StringComparer]::OrdinalIgnoreCase
            )
        }
        [void]$cleanBlobPaths[$record.ObjectId].Add($historicalPath)
    }

    # Inspect each unique clean blob once, including UTF-16 content. Only the
    # category and path are ever reported; matched values remain in memory.
    foreach ($blobEntry in $cleanBlobPaths.GetEnumerator()) {
        $information = $objectInformation[$blobEntry.Key]
        if ($information.Size -gt 25MB) {
            foreach ($historicalPath in $blobEntry.Value) {
                [void]$Violations.Add("historical file exceeds 25 MiB: $historicalPath")
            }
            continue
        }
        $blobBytes = Read-GitBlobBytes -ObjectId $blobEntry.Key
        foreach ($signature in $SecretSignatures.GetEnumerator()) {
            if (-not (Test-BytesForSignature -Bytes $blobBytes -Pattern $signature.Value)) {
                continue
            }
            foreach ($historicalPath in $blobEntry.Value) {
                [void]$Violations.Add(
                    "historical $($signature.Key) signature: $historicalPath"
                )
            }
        }
    }
}
finally {
    Pop-Location
}

if ($Violations.Count -gt 0) {
    Write-Host "Source safety check FAILED ($($Violations.Count) finding(s)):" -ForegroundColor Red
    foreach ($violation in @($Violations) | Sort-Object) {
        Write-Host "  - $violation" -ForegroundColor Red
    }
    exit 1
}

Write-Host "Source safety check passed: no forbidden runtime data or recognized high-confidence credential signatures were found in tracked source or history reachable from HEAD." -ForegroundColor Green
Write-Host "This focused check is not an exhaustive credential scanner." -ForegroundColor DarkGray
exit 0
