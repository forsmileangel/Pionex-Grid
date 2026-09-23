param([string]$RootDir = "D:\My-project\pionex grid record")
$ErrorActionPreference = "Stop"
$RootDir = [IO.Path]::GetFullPath($RootDir)
$name = "Pionex Grid Record - Daily API"
$task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
$runtime = Join-Path $RootDir "runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$backup = Join-Path $runtime ("excel-task-before-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".xml")
Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath $backup -Encoding UTF8
$script = Join-Path $RootDir "pionex-grid-daily.ps1"
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" -WorkingDirectory $RootDir
$trigger = New-ScheduledTaskTrigger -Daily -At "08:12"
Set-ScheduledTask -TaskName $name -Action $action -Trigger $trigger | Out-Null
$updated = Get-ScheduledTask -TaskName $name
if ($updated.Principal.UserId -ne $task.Principal.UserId -or $updated.Principal.LogonType -ne $task.Principal.LogonType) {
    throw "Task principal changed unexpectedly. Original task XML was saved."
}
[pscustomobject]@{ task = $name; start = $updated.Triggers.StartBoundary; backup = $backup } | ConvertTo-Json
