param(
    [int]$Port = 8787
)

if ([Threading.Thread]::CurrentThread.GetApartmentState() -ne "STA") {
    $self = $MyInvocation.MyCommand.Path
    & powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File $self @PSBoundParameters
    exit $LASTEXITCODE
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$RootDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$StartScript = Join-Path $RootDir "start-dashboard-server.ps1"
$Url = "http://127.0.0.1:$Port"

function Test-DashboardUp {
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

function Start-Dashboard {
    if (Test-DashboardUp) { return }
    Stop-Dashboard
    if (-not (Test-Path -LiteralPath $StartScript)) {
        throw "找不到啟動腳本: $StartScript"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $StartScript -Port $Port
    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline) {
        if (Test-DashboardUp) { return }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-DashboardUp)) {
        throw "儀表板已啟動行程，但 http://127.0.0.1:$Port 沒有回應"
    }
}

function Stop-Dashboard {
    foreach ($procId in (Get-DashboardPids)) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 400
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "Pionex Grid 本機儀表板"
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.Width = 460
$form.Height = 280
$form.BackColor = [System.Drawing.Color]::FromArgb(244, 247, 248)
$form.Font = New-Object System.Drawing.Font("Segoe UI", 10)

$title = New-Object System.Windows.Forms.Label
$title.Text = "Pionex Grid — SQLite 本機儀表板"
$title.Left = 20
$title.Top = 16
$title.Width = 410
$title.Height = 28
$title.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($title)

$note = New-Object System.Windows.Forms.Label
$note.Text = "這條線用 SQLite 資料庫記合約網格，網頁在本機 8787。" + [Environment]::NewLine + "另一條是 Excel 每日紀錄（pionex grid record，08:00），不會被這裡開關。"
$note.Left = 20
$note.Top = 48
$note.Width = 410
$note.Height = 52
$note.ForeColor = [System.Drawing.Color]::FromArgb(92, 107, 115)
$form.Controls.Add($note)

$status = New-Object System.Windows.Forms.Label
$status.Left = 20
$status.Top = 108
$status.Width = 410
$status.Height = 28
$status.Font = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($status)

$btnOn = New-Object System.Windows.Forms.Button
$btnOn.Text = "開啟"
$btnOn.Left = 20
$btnOn.Top = 152
$btnOn.Width = 120
$btnOn.Height = 36
$btnOn.BackColor = [System.Drawing.Color]::FromArgb(46, 105, 112)
$btnOn.ForeColor = [System.Drawing.Color]::White
$btnOn.FlatStyle = "Flat"
$form.Controls.Add($btnOn)

$btnOff = New-Object System.Windows.Forms.Button
$btnOff.Text = "關閉"
$btnOff.Left = 156
$btnOff.Top = 152
$btnOff.Width = 120
$btnOff.Height = 36
$btnOff.BackColor = [System.Drawing.Color]::FromArgb(180, 90, 70)
$btnOff.ForeColor = [System.Drawing.Color]::White
$btnOff.FlatStyle = "Flat"
$form.Controls.Add($btnOff)

$btnOpen = New-Object System.Windows.Forms.Button
$btnOpen.Text = "打開網頁"
$btnOpen.Left = 292
$btnOpen.Top = 152
$btnOpen.Width = 130
$btnOpen.Height = 36
$form.Controls.Add($btnOpen)

function Update-Ui {
    $up = Test-DashboardUp
    if ($up) {
        $status.Text = "狀態：已開啟　　$Url"
        $status.ForeColor = [System.Drawing.Color]::FromArgb(31, 122, 77)
        $btnOn.Enabled = $false
        $btnOff.Enabled = $true
        $btnOpen.Enabled = $true
    } else {
        $status.Text = "狀態：已關閉　　開啟後才打得開網頁"
        $status.ForeColor = [System.Drawing.Color]::FromArgb(139, 94, 34)
        $btnOn.Enabled = $true
        $btnOff.Enabled = $false
        $btnOpen.Enabled = $false
    }
}

$btnOn.Add_Click({
    try {
        $btnOn.Enabled = $false
        Start-Dashboard
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "開啟失敗") | Out-Null
    }
    Update-Ui
})

$btnOff.Add_Click({
    try {
        $btnOff.Enabled = $false
        Stop-Dashboard
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "關閉失敗") | Out-Null
    }
    Update-Ui
})

$btnOpen.Add_Click({
    if (Test-DashboardUp) { Start-Process $Url }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.Add_Tick({ Update-Ui })
$form.Add_Shown({ Update-Ui; $timer.Start() })
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })

[void]$form.ShowDialog()
