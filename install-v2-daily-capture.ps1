param(
    [switch]$RunNow,
    [string]$TaskName = "Pionex Grid v2 Daily Capture"
)

$ErrorActionPreference = "Stop"
$RootDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$Python = (Get-Command python -ErrorAction Stop).Source
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $PowerShellPath)) { $PowerShellPath = (Get-Command powershell.exe).Source }

$inner = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"Set-Location -LiteralPath '{0}'; & '{1}' -m v2.capture`"" -f $RootDir.Replace("'", "''"), $Python.Replace("'", "''")
$action = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $inner -WorkingDirectory $RootDir
$startAt = Get-Date -Hour 8 -Minute 5 -Second 0
if ($startAt -le (Get-Date)) { $startAt = $startAt.AddDays(1) }
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "SQLite daily capture at 08:05 Taipei local time, then PATCH pionex-grid-ledger.json. Does not change the v1 08:00 Excel task." -Force | Out-Null

# Interactive tasks skip 08:05 while the PC sits at the login screen (e.g. after a
# Windows Update restart), and StartWhenAvailable does not retry that case.
# Catch-up at logon: --if-missing captures only when today's record is still absent
# and it is past 08:05, so it never replaces a normal 08:05 snapshot.
$CatchUpName = "$TaskName Catch-up"
$catchInner = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"Set-Location -LiteralPath '{0}'; & '{1}' -m v2.capture --if-missing`"" -f $RootDir.Replace("'", "''"), $Python.Replace("'", "''")
$catchAction = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $catchInner -WorkingDirectory $RootDir
$catchTrigger = New-ScheduledTaskTrigger -AtLogOn -User ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME)
$catchTrigger.Delay = "PT2M"
Register-ScheduledTask -TaskName $CatchUpName -Action $catchAction -Trigger $catchTrigger -Settings $settings -Principal $principal -Description "At logon, capture today's SQLite daily record only if the 08:05 run was missed." -Force | Out-Null

if ($RunNow) { Start-ScheduledTask -TaskName $TaskName }
Get-ScheduledTask -TaskName $TaskName, $CatchUpName | Select-Object TaskName, State, Description
