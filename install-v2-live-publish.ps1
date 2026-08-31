param(
    [switch]$RunNow,
    [string]$TaskName = "Pionex Grid v2 Live Publish"
)

$ErrorActionPreference = "Stop"
$RootDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$Python = (Get-Command python -ErrorAction Stop).Source
$PythonW = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
if (-not (Test-Path -LiteralPath $PythonW)) { $PythonW = $Python }
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $PowerShellPath)) { $PowerShellPath = (Get-Command powershell.exe).Source }

$inner = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"Set-Location -LiteralPath '{0}'; `$env:PYTHONPATH = '{0}'; & '{1}' -m v2.capture --live`"" -f $RootDir.Replace("'", "''"), $PythonW.Replace("'", "''")
$action = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $inner -WorkingDirectory $RootDir
$watch = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 8) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $watch -Settings $settings -Principal $principal -Description "Every 30 minutes: fetch current Pionex grids into live-snapshot.json and PATCH pionex-grid-live.json. Does not write the 08:00 Excel or 08:05 daily sqlite ledger." -Force | Out-Null
if ($RunNow) { Start-ScheduledTask -TaskName $TaskName }
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Description
