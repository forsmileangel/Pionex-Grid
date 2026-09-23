param(
    [switch]$DryRun,
    [switch]$AllowStale,
    [switch]$Backfill,
    [string]$SnapshotJson = "",
    [datetime]$ReplaceDate = [datetime]::MinValue,
    [datetime]$CaptureAt = [datetime]::MinValue,
    [string]$RootDir = "D:\My-project\pionex grid record",
    [string]$WorkbookPath = "",
    [string]$V2Root = "D:\My-project\pionex grid record-v2",
    [string]$DatabasePath = ""
)

$ErrorActionPreference = "Stop"
$RootDir = [IO.Path]::GetFullPath($RootDir)
$V2Root = [IO.Path]::GetFullPath($V2Root)
$DataDir = Join-Path $RootDir "data"
$RuntimeDir = Join-Path $RootDir "runtime"
if (-not $WorkbookPath) { $WorkbookPath = Join-Path $DataDir "pionex-grid-history.xlsx" }
if (-not $DatabasePath) { $DatabasePath = Join-Path $V2Root "v2-data\pionex-grid.sqlite" }
$WorkbookPath = [IO.Path]::GetFullPath($WorkbookPath)
$DatabasePath = [IO.Path]::GetFullPath($DatabasePath)
New-Item -ItemType Directory -Force -Path $DataDir, $RuntimeDir | Out-Null
$LogPath = Join-Path $DataDir "pionex-grid-daily.log"
$payloadPath = Join-Path $RuntimeDir ("ledger-" + [guid]::NewGuid().ToString("N") + ".json")
$workFile = Join-Path ([IO.Path]::GetDirectoryName($WorkbookPath)) (".pionex-" + [guid]::NewGuid().ToString("N") + ".xlsx")
$excel = $book = $sheet = $table = $range = $null

function Write-RunLog([string]$Message) {
    Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"), $Message) -Encoding UTF8
}

try {
    # Rebuild from the authoritative ledger so late settlements repair their original dates.
    # Saved API snapshots must first be ingested by v2; Excel must not compute its own deltas.
    if ($SnapshotJson -or $CaptureAt -ne [datetime]::MinValue) {
        throw "Import historical API snapshots into v2 first, then rebuild Excel. Capture timestamps cannot be overridden here."
    }
    $python = "C:\Python314\python.exe"
    if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python.exe -ErrorAction Stop).Source }
    if (-not (Test-Path -LiteralPath (Join-Path $V2Root "v2\legacy_excel.py"))) { throw "v2 Excel ledger adapter is missing." }
    $arguments = @("-B", "-m", "v2.legacy_excel", "--db", $DatabasePath, "--output", $payloadPath)
    if ($AllowStale -or $Backfill) { $arguments += "--allow-stale" }
    Push-Location -LiteralPath $V2Root
    try {
        $output = & $python @arguments 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw $output.Trim() }
    } finally { Pop-Location }
    $payload = Get-Content -LiteralPath $payloadPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($ReplaceDate -ne [datetime]::MinValue -and $payload.dates -notcontains $ReplaceDate.ToString("yyyy-MM-dd")) {
        throw "ReplaceDate is not present in the v2 ledger. No Excel data changed."
    }
    if ($DryRun) {
        [pscustomobject]@{ source = $payload.source; latest_date = $payload.latest_date; dates = $payload.dates.Count; rows = $payload.rows.Count; workbook = $WorkbookPath } | ConvertTo-Json
        exit 0
    }
    if (-not (Test-Path -LiteralPath $WorkbookPath)) { throw "Workbook not found: $WorkbookPath" }
    # Fail before starting Excel if another process already has the target open.
    $fileCheck = [IO.File]::Open($WorkbookPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $fileCheck.Dispose()
    $originalHash = (Get-FileHash -LiteralPath $WorkbookPath -Algorithm SHA256).Hash
    $backup = Join-Path $DataDir ("snapshot-legacy-excel-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff") + ".xlsx")
    Copy-Item -LiteralPath $WorkbookPath -Destination $backup
    Copy-Item -LiteralPath $WorkbookPath -Destination $workFile
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    $book = $excel.Workbooks.Open($workFile, 0, $false)
    foreach ($candidate in $book.Worksheets) {
        try { $table = $candidate.ListObjects.Item("GridLedgerTable"); $sheet = $candidate; break } catch {}
    }
    if ($null -eq $table -or $table.ListColumns.Count -ne 16) { throw "Expected the original 16-column GridLedgerTable." }
    if ($null -ne $table.DataBodyRange) {
        $old = $table.DataBodyRange.Value2
        for ($r = 1; $r -le $table.DataBodyRange.Rows.Count; $r++) {
            if ($null -eq $old[$r, 1] -or "$($old[$r, 1])" -eq "") { continue }
            $day = [datetime]::FromOADate([double]($old[$r, 1])).ToString("yyyy-MM-dd")
            if ($payload.dates -notcontains $day) { throw "Excel contains date $day outside the v2 ledger; refusing to discard it." }
        }
    }
    $headerRow = $table.HeaderRowRange.Row
    $firstColumn = $table.HeaderRowRange.Column
    if ($null -ne $table.DataBodyRange) { $table.DataBodyRange.ClearContents() | Out-Null }
    $range = $sheet.Cells.Item($headerRow, $firstColumn).Resize($payload.rows.Count + 1, 16)
    $table.Resize($range)
    $values = New-Object 'object[,]' $payload.rows.Count,16
    for ($r = 0; $r -lt $payload.rows.Count; $r++) {
        $record = $payload.rows[$r]
        if ($record.Count -ne 16) { throw "Invalid ledger row width." }
        for ($c = 0; $c -lt 16; $c++) {
            $value = $record[$c]
            if ($c -eq 0) { $value = [datetime]::ParseExact([string]$value, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture).ToOADate() }
            elseif ($c -eq 1) { $value = [TimeSpan]::Parse([string]$value).TotalDays }
            elseif ($null -ne $value -and $value -is [ValueType]) { $value = [double]$value }
            $values[$r, $c] = $value
        }
    }
    $table.DataBodyRange.Value2 = $values
    $sheet.Range("A:A").NumberFormat = "yyyy-mm-dd"
    $sheet.Range("B:B").NumberFormat = "hh:mm:ss"
    $sheet.Range("I:M").NumberFormat = "#,##0.00"
    $sheet.Range("A2").Value2 = [string]$payload.note
    $sheet.Range("A2").WrapText = $true
    $sheet.Rows.Item(2).RowHeight = 36
    $sheet.Range("A3").Value2 = [string]$payload.schedule_note
    $book.Save()
    $book.Close($false)
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($book)
    $book = $null
    $excel.Quit()
    if ((Get-FileHash -LiteralPath $WorkbookPath -Algorithm SHA256).Hash -ne $originalHash) {
        throw "Original workbook changed during export; keeping both the original and backup."
    }
    [IO.File]::Replace($workFile, $WorkbookPath, $backup)
    Write-RunLog ("saved source=v2-sqlite; latest={0}; rows={1}; backup={2}" -f $payload.latest_date, $payload.rows.Count, $backup)
    [pscustomobject]@{ ok = $true; latest_date = $payload.latest_date; rows = $payload.rows.Count; workbook = $WorkbookPath; backup = $backup } | ConvertTo-Json
} catch {
    Write-RunLog ("FAILED: " + ($_ | Out-String).Trim())
    Write-Error $_
    exit 1
} finally {
    if ($null -ne $book) { $book.Close($false) }
    if ($null -ne $excel) { $excel.Quit() }
    foreach ($com in @($range, $table, $sheet, $book, $excel)) {
        if ($null -ne $com -and [Runtime.InteropServices.Marshal]::IsComObject($com)) {
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($com)
        }
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    Remove-Item -LiteralPath $payloadPath, $workFile -Force -ErrorAction SilentlyContinue
}
