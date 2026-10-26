param(
    [switch]$RunNow,
    [string]$TaskName = "Pionex Grid v2 Daily Capture"
)

$ErrorActionPreference = "Stop"
$RootDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$Python = (Get-Command python -ErrorAction Stop).Source
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $PowerShellPath)) { $PowerShellPath = (Get-Command powershell.exe).Source }
# powershell.exe -WindowStyle Hidden still flashes a console. The hidden-task-runner starts
# it with no window at all and passes the exit code on, so a failed capture still shows as
# failed and the restart settings below apply. Without the runner, fall back to plain PowerShell.
$PythonW = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
$HiddenRunner = Join-Path $env:USERPROFILE ".local\share\hidden-task-runner\run_hidden.pyw"

function New-PipelineAction([string]$PipelineArgs) {
    $inner = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"Set-Location -LiteralPath '{0}'; & '{1}' -m v2.pipeline{2}; exit `$LASTEXITCODE`"" -f $RootDir.Replace("'", "''"), $Python.Replace("'", "''"), $PipelineArgs
    if ((Test-Path -LiteralPath $PythonW) -and (Test-Path -LiteralPath $HiddenRunner)) {
        return New-ScheduledTaskAction -Execute $PythonW -Argument ("`"{0}`" {1} {2}" -f $HiddenRunner, $PowerShellPath, $inner) -WorkingDirectory $RootDir
    }
    return New-ScheduledTaskAction -Execute $PowerShellPath -Argument $inner -WorkingDirectory $RootDir
}

$action = New-PipelineAction ""
$startAt = Get-Date -Hour 8 -Minute 15 -Second 0
if ($startAt -le (Get-Date)) { $startAt = $startAt.AddDays(1) }
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
# A failed capture (exit 1) is retried three times, 10 minutes apart: one HTTP 502 at 08:15
# would otherwise lose the day. The 30-minute limit covers the collector (stopped after
# 5 minutes) plus the Excel export (stopped after 10), so a hung run cannot block the next
# day's start. Excel failures exit 0 and are not retried.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "08:15 Taipei: capture into SQLite, publish the ledger, then export both Excel workbooks. Does not disable the old 08:10 and 08:12 tasks." -Force | Out-Null

# Interactive tasks skip 08:15 while the PC sits at the login screen (e.g. after a
# Windows Update restart), and StartWhenAvailable does not retry that case.
# Catch-up at logon runs the same pipeline. --if-missing captures only when today's
# record is still absent and it is past 08:15, so it never replaces a normal 08:15
# snapshot. Excel export still runs when the ledger date is newer than the last export.
$CatchUpName = "$TaskName Catch-up"
$catchAction = New-PipelineAction " --if-missing"
$catchTrigger = New-ScheduledTaskTrigger -AtLogOn -User ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME)
$catchTrigger.Delay = "PT2M"
Register-ScheduledTask -TaskName $CatchUpName -Action $catchAction -Trigger $catchTrigger -Settings $settings -Principal $principal -Description "At logon, run the daily pipeline. Capture only if the 08:15 record was missed; still export Excel when the ledger is newer." -Force | Out-Null

if ($RunNow) { Start-ScheduledTask -TaskName $TaskName }
Get-ScheduledTask -TaskName $TaskName, $CatchUpName | Select-Object TaskName, State, Description
