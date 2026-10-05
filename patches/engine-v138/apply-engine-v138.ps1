param(
    [Parameter(Mandatory=$true)][string]$SourceRoot,
    [Parameter(Mandatory=$true)][string]$SourceTar
)
$ErrorActionPreference = 'Stop'
$metadata = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'metadata.json') -Raw -Encoding utf8 | ConvertFrom-Json
$actualTarHash = (Get-FileHash -LiteralPath $SourceTar -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualTarHash -ne $metadata.source_archive_sha256) { throw 'Source archive SHA-256 does not match the recorded v0.1.38 base.' }
foreach ($entry in $metadata.files) {
    $path = Join-Path $SourceRoot $entry.path
    if ($entry.kind -eq 'added') {
        if (Test-Path -LiteralPath $path) { throw "Refusing patch: added source already exists: $($entry.path)" }
        continue
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Base source is missing: $($entry.path)" }
    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    $normalized = $text.Replace("`r`n", "`n").Replace("`r", "`n")
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $actual = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    if ($actual -ne $entry.base_sha256) { throw "Base source hash mismatch: $($entry.path)" }
}
$patch = Join-Path $PSScriptRoot 'engine-v138.patch'
& git -C $SourceRoot apply --check $patch
if ($LASTEXITCODE -ne 0) { throw 'git apply --check failed; source root is not the pristine v0.1.38 base.' }
& git -C $SourceRoot apply $patch
if ($LASTEXITCODE -ne 0) { throw 'git apply failed.' }
Write-Output 'Applied the source-only v138 patch. This does not build or attest an engine binary.'
