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
if ($RunNow) { Start-ScheduledTask -TaskName $TaskName }
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Description
