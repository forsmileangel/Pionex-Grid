param(
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"
$RootDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
Set-Location -LiteralPath $RootDir
$env:PYTHONPATH = $RootDir

$LogDir = Join-Path $RootDir "v2-data"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogPath = Join-Path $LogDir "dashboard.log"

function Write-DashLog([string]$Message) {
    Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"), $Message) -Encoding UTF8
}

function Test-DashboardHttp {
    try {
        $res = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/v1/status" -TimeoutSec 5 -UseBasicParsing
        return $res.StatusCode -ge 200 -and $res.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Get-DashboardPids {
    $pids = @()
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "^python(w)?\.exe$" -and $_.CommandLine -match "v2\.server" } |
            ForEach-Object { $pids += [int]$_.ProcessId }
    } catch {}
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { $pids += [int]$_.OwningProcess }
    } catch {}
    return $pids | Where-Object { $_ -gt 0 } | Select-Object -Unique
}

function Start-DetachedPython([string]$Exe, [string]$ArgLine, [string]$Cwd) {
    $type = @"
using System;
using System.Runtime.InteropServices;
using System.ComponentModel;
public static class DashProc {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern bool CreateProcess(string app, string cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct STARTUPINFO {
        public int cb; public string reserved, desktop, title;
        public int x, y, xSize, ySize, xCount, yCount, fill, flags;
        public short show, reserved2;
        public IntPtr reserved2Ptr, stdin, stdout, stderr;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr process, thread; public int pid, tid; }
    const uint CREATE_BREAKAWAY_FROM_JOB = 0x01000000;
    const uint CREATE_NO_WINDOW = 0x08000000;
    const uint CREATE_NEW_PROCESS_GROUP = 0x00000200;
    public static int Start(string exe, string args, string cwd) {
        STARTUPINFO si = new STARTUPINFO();
        si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        PROCESS_INFORMATION pi;
        string cmd = "\"" + exe + "\" " + args;
        uint flags = CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP;
        if (!CreateProcess(null, cmd, IntPtr.Zero, IntPtr.Zero, false, flags, IntPtr.Zero, cwd, ref si, out pi)) {
            int err = Marshal.GetLastWin32Error();
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP;
            if (!CreateProcess(null, cmd, IntPtr.Zero, IntPtr.Zero, false, flags, IntPtr.Zero, cwd, ref si, out pi)) {
                throw new Win32Exception(err);
            }
        }
        if (pi.process != IntPtr.Zero) CloseHandle(pi.process);
        if (pi.thread != IntPtr.Zero) CloseHandle(pi.thread);
        return pi.pid;
    }
}
"@
    if (-not ([System.Management.Automation.PSTypeName]"DashProc").Type) {
        Add-Type -TypeDefinition $type
    }
    return [DashProc]::Start($Exe, $ArgLine, $Cwd)
}

if (Test-DashboardHttp) {
    Write-DashLog "already serving http://127.0.0.1:$Port"
    exit 0
}

$stale = @(Get-DashboardPids)
if ($stale.Count -gt 0) {
    Write-DashLog ("port $Port hung; stopping pids " + ($stale -join ","))
    foreach ($procId in $stale) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 800
}

$python = $null
foreach ($name in @("pythonw.exe", "python.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($null -ne $cmd) {
        $python = $cmd.Source
        break
    }
}
if ([string]::IsNullOrWhiteSpace($python)) {
    throw "python was not found on PATH. Install Python or add it to PATH so the dashboard can start at logon."
}

$pidStarted = 0
try {
    $pidStarted = Start-DetachedPython $python "-m v2.server --port $Port" $RootDir
    Write-DashLog ("started {0} pid {1} (detached)" -f $python, $pidStarted)
} catch {
    Write-DashLog ("detached start failed: {0}; falling back to Start-Process" -f $_.Exception.Message)
    $start = @{
        FilePath = $python
        ArgumentList = @("-m", "v2.server", "--port", "$Port")
        WorkingDirectory = $RootDir
        WindowStyle = "Hidden"
        PassThru = $true
    }
    if (-not $python.ToLower().EndsWith("pythonw.exe")) {
        $start.RedirectStandardOutput = Join-Path $LogDir "dashboard-out.log"
        $start.RedirectStandardError = Join-Path $LogDir "dashboard-err.log"
    }
    $proc = Start-Process @start
    $pidStarted = $proc.Id
    Write-DashLog ("started {0} pid {1}" -f $python, $pidStarted)
}

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    if (Test-DashboardHttp) {
        Write-DashLog ("serving http://127.0.0.1:$Port pid {0}" -f $pidStarted)
        exit 0
    }
    Start-Sleep -Milliseconds 300
}
Write-DashLog ("started pid {0} but http://127.0.0.1:{1} did not respond" -f $pidStarted, $Port)
exit 1
