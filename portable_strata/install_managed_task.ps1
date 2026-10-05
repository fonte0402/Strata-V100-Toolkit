param([Parameter(Mandatory=$true)][string]$ArgumentsPath)
$ErrorActionPreference='Stop'
$taskPayload=Get-Content -LiteralPath $ArgumentsPath -Raw -Encoding utf8 | ConvertFrom-Json
$taskName=[string]$taskPayload.taskName
if ([string]::IsNullOrWhiteSpace($taskName)) { throw 'taskName is required.' }
$existing=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and $existing.Description -notlike 'Portable Strata managed service*') {
    throw "Refusing to replace unrelated scheduled task: $taskName"
}
$taskUser=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction=New-ScheduledTaskAction -Execute $taskPayload.execute -Argument $taskPayload.arguments -WorkingDirectory $taskPayload.workingDirectory
$taskPrincipal=New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskSettings.Hidden=$true
$taskDefinition=New-ScheduledTask -Action $taskAction -Principal $taskPrincipal -Settings $taskSettings -Description 'Portable Strata managed service; direct Python supervisor, no automatic restart loop. Interactive user only.'
Register-ScheduledTask -TaskName $taskName -InputObject $taskDefinition -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'Managed Strata task dispatched.'
