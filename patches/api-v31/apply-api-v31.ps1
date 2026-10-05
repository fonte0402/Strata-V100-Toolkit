param(
    [Parameter(Mandatory=$true)][string]$Repo,
    [Parameter(Mandatory=$true)][string]$PatchPath
)
$ErrorActionPreference = 'Stop'
$expected = '9259cad4cfa3543cd3b8decab5962672b968c649'
$actual = (& git -C $Repo rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot read target Git HEAD.' }
if ($actual -ne $expected) { throw "Refusing patch: target HEAD $actual does not equal required base $expected." }
& git -C $Repo apply --check $PatchPath
if ($LASTEXITCODE -ne 0) { throw 'git apply --check failed; target source is not a clean matching base.' }
& git -C $Repo apply $PatchPath
if ($LASTEXITCODE -ne 0) { throw 'git apply failed.' }
Write-Output "Applied API patch to $expected."
