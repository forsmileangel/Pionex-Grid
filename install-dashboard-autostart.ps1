param(
    [switch]$RunNow,
    [string]$TaskName = "Pionex Grid SQLite Dashboard"
)

$ErrorActionPreference = "Stop"
$RootDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$ScriptPath = Join-Path $RootDir "start-dashboard-server.ps1"
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path -LiteralPath $ScriptPath)) { throw "Dashboard start script not found: $ScriptPath" }
if (-not (Test-Path -LiteralPath $PowerShellPath)) { $PowerShellPath = (Get-Command powershell.exe).Source }

$action = New-ScheduledTaskAction -Execute $PowerShellPath -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"{0}`"" -f $ScriptPath) -WorkingDirectory $RootDir
$userId = "{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME
$logon = New-ScheduledTaskTrigger -AtLogOn -User $userId
$watch = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(20)) -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

foreach ($old in @("Pionex Grid v2 Dashboard", "Pionex Grid SQLite 儀表板")) {
    Unregister-ScheduledTask -TaskName $old -Confirm:$false -ErrorAction SilentlyContinue
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $watch) -Settings $settings -Principal $principal -Description "Keep SQLite local dashboard at http://127.0.0.1:8787 running: start at logon and re-check every 30 minutes. Not the 08:00 Excel recorder." -Force | Out-Null
if ($RunNow) { Start-ScheduledTask -TaskName $TaskName }
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Description
Get-ScheduledTask -TaskName $TaskName | Select-Object -ExpandProperty Triggers | ForEach-Object { $_ | Select-Object Enabled, StartBoundary, Repetition }
