param(
    [string]$TaskName = "Pionex Grid v2 Shadow 0815"
)

# Trial ended 2026-10-26: 08:15 caught every 08:00 funding settlement, so the daily capture
# moved to 08:15 and this task is disabled. Kept to rerun a comparison if ever needed.
# Trial (from 2026-10-20): an 08:15 snapshot into v2-data\shadow only, to measure 08:00
# funding that posts after the 08:05 capture. Writes no ledger, Gist or Excel.
# Remove with: Unregister-ScheduledTask -TaskName "Pionex Grid v2 Shadow 0815" -Confirm:$false

$ErrorActionPreference = "Stop"
$RootDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$Python = (Get-Command python -ErrorAction Stop).Source
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $PowerShellPath)) { $PowerShellPath = (Get-Command powershell.exe).Source }
$PythonW = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
$HiddenRunner = Join-Path $env:USERPROFILE ".local\share\hidden-task-runner\run_hidden.pyw"

$inner = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"Set-Location -LiteralPath '{0}'; & '{1}' -m v2.shadow; exit `$LASTEXITCODE`"" -f $RootDir.Replace("'", "''"), $Python.Replace("'", "''")
if ((Test-Path -LiteralPath $PythonW) -and (Test-Path -LiteralPath $HiddenRunner)) {
    $action = New-ScheduledTaskAction -Execute $PythonW -Argument ("`"{0}`" {1} {2}" -f $HiddenRunner, $PowerShellPath, $inner) -WorkingDirectory $RootDir
} else {
    $action = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $inner -WorkingDirectory $RootDir
}
$startAt = Get-Date -Hour 8 -Minute 15 -Second 0
if ($startAt -le (Get-Date)) { $startAt = $startAt.AddDays(1) }
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
# No StartWhenAvailable: a late start would not measure 08:15, and v2.shadow skips
# anything outside 08:10-08:30 anyway.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Trial: 08:15 shadow capture into v2-data\shadow only (no ledger, Gist or Excel). Measures late 08:00 funding settlements." -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Description
