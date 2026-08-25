param(
    [switch]$DryRun,
    [switch]$Backfill,
    [string]$SnapshotJson = "",
    [string]$RootDir = "D:\My-project\pionex grid record",
    [string]$CredentialPath = "",
    [string]$ApiScriptPath = "",
    [string]$NodePath = "",
    [datetime]$ReplaceDate = [datetime]::MinValue,
    [datetime]$CaptureAt = [datetime]::MinValue,
    [string]$WorkbookPath = ""
)

$ErrorActionPreference = "Stop"
$RootDir = [System.IO.Path]::GetFullPath($RootDir)
$DataDir = Join-Path $RootDir "data"
$RuntimeDir = Join-Path $RootDir "runtime"
if ([string]::IsNullOrWhiteSpace($CredentialPath)) { $CredentialPath = Join-Path $RootDir "PIONEX API.txt" }
if ([string]::IsNullOrWhiteSpace($ApiScriptPath)) { $ApiScriptPath = Join-Path $RootDir "pionex-grid-api.mjs" }
if ([string]::IsNullOrWhiteSpace($WorkbookPath)) { $WorkbookPath = Join-Path $DataDir "pionex-grid-history.xlsx" }
if (-not [System.IO.Path]::IsPathRooted($CredentialPath)) { $CredentialPath = [System.IO.Path]::GetFullPath($CredentialPath) }
if (-not [System.IO.Path]::IsPathRooted($ApiScriptPath)) { $ApiScriptPath = [System.IO.Path]::GetFullPath($ApiScriptPath) }
if (-not [System.IO.Path]::IsPathRooted($WorkbookPath)) { $WorkbookPath = [System.IO.Path]::GetFullPath($WorkbookPath) }
$LogPath = Join-Path $DataDir "pionex-grid-daily.log"

function U([string]$Hex) {
    $chars = foreach ($part in ($Hex -split "\s+" | Where-Object { $_ })) {
        if ($part -match "^[0-9A-Fa-f]{1,8}$") { [char]([Convert]::ToInt32([string]$part, 16)) } else { [string]$part }
    }
    return -join $chars
}

$T = @{
    GridRecord = U "7db2 683c"
    DailyTotal = U "65e5 7e3d 8a08"
    Baseline = U "57fa 6e96 65e5"
    New = U "65b0 589e"
    Continue = U "6301 7e8c"
    Closed = U "95dc 5009"
    Yes = U "662f"
    No = U "5426"
    TotalSymbol = U "5408 7d04 7db2 683c 5408 8a08"
    ContractProduct = U "5408 7d04 7db2 683c"
    CoinProduct = U "5e63 672c 4f4d 5408 7d04 7db2 683c"
    Long = U "505a 591a"
    Short = U "505a 7a7a"
    GridSheet = U "6bcf 65e5 7db2 683c 5dee 984d"
    SavingsSheet = U "5c6f 5e63 5bf6 8a18 9304"
    SavingsTitle = U "5c6f 5e63 5bf6 8a18 9304"
    SavingsNote = "Savings data skipped: Pionex public API did not return this product. It is excluded from contract-grid totals."
    Source = "Pionex API (read-only)"
    ManualSource = "Pionex API (manual backfill)"
}

$GridHeaders = @(
    U "65e5 671f", U "6642 9593", U "8a18 9304 985e 578b", ((U "7db2 683c") + " Key"), U "4ea4 6613 5c0d", U "5efa 7acb 6642 9593", U "7522 54c1 985e 578b", U "72c0 614b",
    ((U "4eca 65e5 7db2 683c 5229 6f64") + " (USDT)"), ((U "524d 6b21 7db2 683c 5229 6f64") + " (USDT)"), ((U "6bcf 65e5 5229 6f64") + " (USDT)"), ((U "7d2f 8a08 5229 6f64") + " (USDT)"), ((U "5be6 969b 6295 8cc7 984d") + " (USDT)"), U "7d0d 5165 6bcf 65e5 7e3d 8a08", U "5b8c 6574 5361 7247 6578 6574", U "4f86 6e90"
)
$SavingsHeaders = @(U "65e5 671f", U "6642 9593", U "8a18 9304 985e 578b", U "4f86 6e90", U "72c0 614b", U "8aaa 660e")

New-Item -ItemType Directory -Force -Path $DataDir, $RuntimeDir | Out-Null

function Write-RunLog([string]$Message) {
    Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"), $Message) -Encoding UTF8
}

function Resolve-NodePath {
    if (-not [string]::IsNullOrWhiteSpace($NodePath) -and (Test-Path -LiteralPath $NodePath)) { return (Resolve-Path -LiteralPath $NodePath).Path }
    if (-not [string]::IsNullOrWhiteSpace($env:PIONEX_NODE_PATH) -and (Test-Path -LiteralPath $env:PIONEX_NODE_PATH)) { return (Resolve-Path -LiteralPath $env:PIONEX_NODE_PATH).Path }
    $bundled = "C:\Users\Forever you\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    if (Test-Path -LiteralPath $bundled) { return $bundled }
    $command = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) { return $command.Source }
    throw "Node.js was not found. Set PIONEX_NODE_PATH or pass -NodePath."
}

function Convert-ExcelDecimal($Value) {
    if ($null -eq $Value -or "$Value" -eq "") { return $null }
    if ($Value -is [ValueType]) { try { return [decimal]$Value } catch {} }
    try { return [decimal]::Parse("$Value", [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture) } catch { return $null }
}

function Convert-ExcelDate($Value) {
    if ($null -eq $Value -or "$Value" -eq "") { return $null }
    if ($Value -is [datetime]) { return $Value.Date }
    try { return [datetime]::FromOADate([double]$Value).Date } catch {}
    try { return ([datetime]::Parse("$Value")).Date } catch { return $null }
}

function Round-Amount($Value) {
    if ($null -eq $Value) { return $null }
    return [decimal]::Round([decimal]$Value, 8, [MidpointRounding]::AwayFromZero)
}

function Invoke-ApiCapture([datetime]$At) {
    if (-not (Test-Path -LiteralPath $CredentialPath)) { throw "Credential file not found: $CredentialPath" }
    if (-not (Test-Path -LiteralPath $ApiScriptPath)) { throw "API collector not found: $ApiScriptPath" }
    $node = Resolve-NodePath
    $output = & $node $ApiScriptPath "--credentials" $CredentialPath 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw ("API collector failed: " + $output.Trim()) }
    try { $snapshot = $output.Trim() | ConvertFrom-Json } catch { throw "API collector returned invalid JSON." }
    $items = @($snapshot.Records)
    $expected = 0
    try { $expected = [int]$snapshot.ExpectedCardCount } catch {}
    if ($expected -le 0 -or $items.Count -eq 0 -or $expected -ne $items.Count) { throw "API capture is incomplete: expected $expected records, received $($items.Count)." }
    $seen = @{}
    $records = New-Object System.Collections.Generic.List[object]
    foreach ($item in $items) {
        $key = [string]$item.Key
        $symbol = [string]$item.Symbol
        $created = [string]$item.Created
        $investment = Convert-ExcelDecimal $item.Investment
        $gridProfit = Convert-ExcelDecimal $item.GridProfit
        if ([string]::IsNullOrWhiteSpace($key) -or [string]::IsNullOrWhiteSpace($symbol) -or [string]::IsNullOrWhiteSpace($created) -or $null -eq $investment -or $null -eq $gridProfit) { throw "API capture contains an incomplete grid record." }
        if ($seen.ContainsKey($key)) { throw "API capture contains duplicate grid key: $key" }
        $seen[$key] = $true
        [void]$records.Add([pscustomobject]@{
            Key = $key; CardType = "Contract"; Symbol = $symbol; Created = $created; Product = [string]$item.Product; Leverage = [string]$item.Leverage
            Investment = $investment; GridProfit = $gridProfit; TotalProfit = Convert-ExcelDecimal $item.TotalProfit; IsComplete = $true; Missing = ""
        })
    }
    $recordArray = $records.ToArray()
    return [pscustomobject]@{
        CaptureAt = $At; ExpectedCardCount = $expected; ContractCount = $recordArray.Count; SavingsCount = 0; AllCards = $recordArray; Records = $recordArray; ScreenCount = 0; Source = $T.Source
    }
}

function Import-SnapshotCapture([string]$Path, [datetime]$RequestedAt) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Snapshot JSON not found: $Path" }
    try { $snapshot = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { throw "Snapshot JSON cannot be read." }
    $items = @($snapshot.Records)
    $expected = 0
    try { $expected = [int]$snapshot.ExpectedCardCount } catch {}
    if ($expected -le 0 -or $items.Count -eq 0 -or $expected -ne $items.Count) { throw "Snapshot JSON is incomplete: expected $expected records, received $($items.Count)." }
    $at = $RequestedAt
    if ($at -eq [datetime]::MinValue) { try { $at = Get-Date $snapshot.CapturedAt } catch { throw "Snapshot JSON has no valid capture time." } }
    $seen = @{}
    $records = New-Object System.Collections.Generic.List[object]
    foreach ($item in $items) {
        $key = [string]$item.Key
        $symbol = [string]$item.Symbol
        $created = [string]$item.Created
        $investment = Convert-ExcelDecimal $item.Investment
        $gridProfit = Convert-ExcelDecimal $item.GridProfit
        if ([string]::IsNullOrWhiteSpace($key) -or [string]::IsNullOrWhiteSpace($symbol) -or [string]::IsNullOrWhiteSpace($created) -or $null -eq $investment -or $null -eq $gridProfit) { throw "Snapshot JSON contains an incomplete grid record." }
        if ($seen.ContainsKey($key)) { throw "Snapshot JSON contains duplicate grid key: $key" }
        $seen[$key] = $true
        [void]$records.Add([pscustomobject]@{ Key = $key; CardType = "Contract"; Symbol = $symbol; Created = $created; Product = [string]$item.Product; Leverage = [string]$item.Leverage; Investment = $investment; GridProfit = $gridProfit; TotalProfit = Convert-ExcelDecimal $item.TotalProfit; IsComplete = $true; Missing = "" })
    }
    $recordArray = $records.ToArray()
    return [pscustomobject]@{ CaptureAt = $at; ExpectedCardCount = $expected; ContractCount = $recordArray.Count; SavingsCount = 0; AllCards = $recordArray; Records = $recordArray; ScreenCount = 0; Source = $T.ManualSource }
}

function Add-ExcelTableRow($Table, [object[]]$Values) {
    $range = $Table.ListRows.Add().Range
    for ($i = 0; $i -lt $Values.Count; $i++) {
        $cell = $range.Cells.Item(1, $i + 1)
        try {
            if ($null -eq $Values[$i]) { $cell.ClearContents() }
            elseif ($Values[$i] -is [datetime]) { $cell.Value = $Values[$i] }
            elseif ($Values[$i] -is [double] -or $Values[$i] -is [single] -or $Values[$i] -is [decimal] -or $Values[$i] -is [int]) { $cell.Value2 = [double]$Values[$i] }
            else { $cell.Value2 = [string]$Values[$i] }
        } catch {
            throw "Excel cell write failed at column $($i + 1), value type $($Values[$i].GetType().FullName): $($_.Exception.Message)"
        }
    }
}

function Get-TableBodyRows($Table, [int]$ColumnCount) {
    $body = $Table.DataBodyRange
    if ($null -eq $body) { return @() }
    $rows = New-Object System.Collections.Generic.List[object]
    for ($r = 1; $r -le $body.Rows.Count; $r++) {
        $values = New-Object object[] $ColumnCount
        for ($c = 1; $c -le $ColumnCount; $c++) { $values[$c - 1] = $body.Cells.Item($r, $c).Value2 }
        [void]$rows.Add($values)
    }
    return $rows.ToArray()
}

function Get-LedgerState($Rows, [datetime]$RunDate) {
    $byKey = @{}
    $latestTotal = $null
    for ($index = 0; $index -lt $Rows.Count; $index++) {
        $row = $Rows[$index]
        $date = Convert-ExcelDate $row[0]
        if ($null -eq $date -or $date -ge $RunDate.Date) { continue }
        $type = [string]$row[2]
        if ($type -eq $T.DailyTotal) {
            $candidate = [pscustomobject]@{ Date = $date; Sequence = $index; Cumulative = Convert-ExcelDecimal $row[11] }
            if ($null -eq $latestTotal -or $candidate.Date -gt $latestTotal.Date -or ($candidate.Date -eq $latestTotal.Date -and $candidate.Sequence -gt $latestTotal.Sequence)) { $latestTotal = $candidate }
            continue
        }
        if ($type -ne $T.GridRecord) { continue }
        $key = [string]$row[3]
        if ([string]::IsNullOrWhiteSpace($key) -or $key -eq "__DAILY_TOTAL__") { continue }
        $candidate = [pscustomobject]@{ Date = $date; Sequence = $index; Active = ([string]$row[7] -ne $T.Closed); GridProfit = Convert-ExcelDecimal $row[8]; Cumulative = Convert-ExcelDecimal $row[11]; Symbol = [string]$row[4]; Created = [string]$row[5]; Product = [string]$row[6]; Investment = Convert-ExcelDecimal $row[12] }
        if ($null -eq $candidate.Cumulative) { $candidate.Cumulative = [decimal]0 }
        if (-not $byKey.ContainsKey($key) -or $candidate.Date -gt $byKey[$key].Date -or ($candidate.Date -eq $byKey[$key].Date -and $candidate.Sequence -gt $byKey[$key].Sequence)) { $byKey[$key] = $candidate }
    }
    return [pscustomobject]@{ ByKey = $byKey; LatestTotal = $latestTotal }
}

function Get-ProductLabel([string]$Product) {
    if ($Product -eq "coin_margined_contract_grid") { return $T.CoinProduct }
    return $T.ContractProduct
}

function Get-LeverageLabel([string]$Value) {
    return $Value.Replace(" long", (" " + $T.Long)).Replace(" short", (" " + $T.Short))
}

function Ensure-SavingsWorksheet($Book) {
    $sheet = $null
    try { $sheet = $Book.Worksheets.Item($T.SavingsSheet) } catch {}
    if ($null -eq $sheet) {
        $sheet = $Book.Worksheets.Add()
        $sheet.Name = $T.SavingsSheet
        $sheet.Range("A1:F1").Merge()
        $sheet.Range("A1").Value2 = $T.SavingsTitle
        $sheet.Range("A2:F2").Merge()
        $sheet.Range("A2").Value2 = $T.SavingsNote
        for ($i = 0; $i -lt $SavingsHeaders.Count; $i++) { $sheet.Cells.Item(4, $i + 1).Value2 = $SavingsHeaders[$i] }
        $sheet.ListObjects.Add(1, $sheet.Range("A4:F4"), $null, 1).Name = "SavingsLedgerTable"
        $sheet.Range("A1:F1").Font.Bold = $true
        $sheet.Range("A1:F1").Interior.Color = 0x263238
        $sheet.Range("A1:F1").Font.Color = 0xFFFFFF
        $sheet.Range("A2:F2").WrapText = $true
        $sheet.Range("A:A").ColumnWidth = 14; $sheet.Range("B:B").ColumnWidth = 12; $sheet.Range("C:C").ColumnWidth = 16; $sheet.Range("D:D").ColumnWidth = 22; $sheet.Range("E:E").ColumnWidth = 14; $sheet.Range("F:F").ColumnWidth = 72
        $sheet.Rows.Item(2).RowHeight = 42
    }
    return $sheet
}

function Update-Workbook($Capture) {
    if (-not (Test-Path -LiteralPath $WorkbookPath)) { throw "Workbook not found: $WorkbookPath" }
    $runDate = $Capture.CaptureAt.Date
    $backup = "$WorkbookPath.bak"
    Copy-Item -LiteralPath $WorkbookPath -Destination $backup -Force
    $excel = $null; $book = $null; $failure = $null
    try {
        $excel = New-Object -ComObject Excel.Application
        $excel.Visible = $false; $excel.DisplayAlerts = $false
        $book = $excel.Workbooks.Open($WorkbookPath, 0, $false)
        [void](Ensure-SavingsWorksheet $book)
        $sheet = $book.Worksheets.Item($T.GridSheet)
        $table = $sheet.ListObjects.Item("GridLedgerTable")
        $rows = @(Get-TableBodyRows $table 16)
        if ($ReplaceDate -ne [datetime]::MinValue -and $ReplaceDate.Date -ne $runDate) { throw "ReplaceDate must match the capture date." }
        foreach ($row in $rows) { if ((Convert-ExcelDate $row[0]) -eq $runDate -and $ReplaceDate -eq [datetime]::MinValue) { throw "A record for $($runDate.ToString('yyyy-MM-dd')) already exists. No duplicate written." } }

        $state = Get-LedgerState $rows $runDate
        $previousByKey = $state.ByKey
        $currentKeys = @{}
        $pending = New-Object System.Collections.Generic.List[object[]]
        $totalInvestment = [decimal]0; $totalGridProfit = [decimal]0; $previousTotal = [decimal]0; $dailyTotal = [decimal]0; $matched = 0

        foreach ($record in $Capture.Records) {
            if ($currentKeys.ContainsKey($record.Key)) { throw "Duplicate current grid key detected." }
            $currentKeys[$record.Key] = $true
            $investment = Round-Amount $record.Investment; $gridProfit = Round-Amount $record.GridProfit
            $totalInvestment += $investment; $totalGridProfit += $gridProfit
            $previous = $null; $before = [decimal]0; $status = $T.New
            if ($previousByKey.ContainsKey($record.Key) -and $previousByKey[$record.Key].Active) {
                $previous = $previousByKey[$record.Key].GridProfit
                if ($null -eq $previous) { throw "Previous grid profit is missing for $($record.Key)." }
                $previous = [decimal]$previous; $before = [decimal]$previousByKey[$record.Key].Cumulative; $previousTotal += $previous; $matched++; $status = $T.Continue
            }
            if ($null -eq $previous) { $status = if ($rows.Count -eq 0 -or $previousByKey.Count -eq 0) { $T.Baseline } else { $T.New } }
            $daily = if ($null -eq $previous) { [decimal]0 } else { Round-Amount ($gridProfit - $previous) }
            $cumulative = Round-Amount ($before + $daily); $dailyTotal += $daily
            $displayProduct = Get-ProductLabel ([string]$record.Product)
            $displayLeverage = Get-LeverageLabel ([string]$record.Leverage)
            $pending.Add(@($runDate, $Capture.CaptureAt.ToString("HH:mm:ss"), $T.GridRecord, [string]$record.Key, [string]$record.Symbol, [string]$record.Created, (($displayProduct + " " + $displayLeverage).Trim()), $status, [double]$gridProfit, $(if ($null -eq $previous) { $null } else { [double]$previous }), [double]$daily, [double]$cumulative, [double]$investment, $T.Yes, [int]$Capture.ExpectedCardCount, $Capture.Source))
        }

        foreach ($key in @($previousByKey.Keys)) {
            $old = $previousByKey[$key]
            if (-not $old.Active -or $currentKeys.ContainsKey($key)) { continue }
            $pending.Add(@($runDate, $Capture.CaptureAt.ToString("HH:mm:ss"), $T.GridRecord, $key, $old.Symbol, $old.Created, $old.Product, $T.Closed, $null, $(if ($null -eq $old.GridProfit) { $null } else { [double]$old.GridProfit }), $null, [double]$old.Cumulative, $null, $T.No, [int]$Capture.ExpectedCardCount, $Capture.Source))
        }

        $totalInvestment = Round-Amount $totalInvestment
        $totalGridProfit = Round-Amount $totalGridProfit
        $previousTotal = Round-Amount $previousTotal
        $dailyTotal = Round-Amount $dailyTotal
        $beforeGlobal = if ($null -ne $state.LatestTotal -and $null -ne $state.LatestTotal.Cumulative) { Round-Amount $state.LatestTotal.Cumulative } else { [decimal]0 }
        $pending.Add(@($runDate, $Capture.CaptureAt.ToString("HH:mm:ss"), $T.DailyTotal, "__DAILY_TOTAL__", $T.TotalSymbol, "", $T.ContractProduct, $T.DailyTotal, [double]$totalGridProfit, $(if ($matched -eq 0) { $null } else { [double]$previousTotal }), [double]$dailyTotal, [double](Round-Amount ($beforeGlobal + $dailyTotal)), [double]$totalInvestment, $T.No, [int]$Capture.ExpectedCardCount, $Capture.Source))

        if ($ReplaceDate -ne [datetime]::MinValue) {
            for ($rowIndex = $table.ListRows.Count; $rowIndex -ge 1; $rowIndex--) {
                if ((Convert-ExcelDate $table.ListRows.Item($rowIndex).Range.Cells.Item(1, 1).Value2) -eq $ReplaceDate.Date) { $table.ListRows.Item($rowIndex).Delete() }
            }
        }
        foreach ($values in $pending) { Add-ExcelTableRow $table $values }
        $sheet.Range("A:A").NumberFormat = "yyyy-mm-dd"
        $sheet.Range("B:B").NumberFormat = "hh:mm:ss"
        $sheet.Range("F:F").NumberFormat = "yyyy-mm-dd hh:mm:ss"
        $sheet.Range("I:M").NumberFormat = "#,##0.00"
        $book.Save()
        Write-RunLog ("saved date={0}; contracts={1}; closed={2}; matched={3}; dailyTotal={4}; cumulativeTotal={5}; savings=skipped" -f $runDate.ToString("yyyy-MM-dd"), $Capture.Records.Count, ($pending.Count - $Capture.Records.Count - 1), $matched, $dailyTotal, ($beforeGlobal + $dailyTotal))
    } catch { $failure = $_ }
    finally {
        if ($null -ne $book) { $book.Close($false) }
        if ($null -ne $excel) { $excel.Quit() }
        if ($null -ne $book) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($book) }
        if ($null -ne $excel) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($excel) }
        [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    }
    if ($null -ne $failure) { if (Test-Path -LiteralPath $backup) { try { Copy-Item -LiteralPath $backup -Destination $WorkbookPath -Force } catch {} }; throw $failure }
}

try {
    $captureAt = if ($CaptureAt -eq [datetime]::MinValue) { Get-Date } else { $CaptureAt }
    if ($Backfill -and [string]::IsNullOrWhiteSpace($SnapshotJson) -and $captureAt.Date -ne (Get-Date).Date) { throw "Without a saved snapshot, manual backfill can only record today's current API state." }
    Write-RunLog ("start dryRun={0}; backfill={1}; captureAt={2}; source=API-only" -f $DryRun.IsPresent, $Backfill.IsPresent, $captureAt.ToString("yyyy-MM-dd HH:mm:ss"))
    $capture = if (-not [string]::IsNullOrWhiteSpace($SnapshotJson)) { Import-SnapshotCapture $SnapshotJson $CaptureAt } else { Invoke-ApiCapture $captureAt }
    if ($Backfill) { $capture.Source = $T.ManualSource }
    Write-RunLog ("capture complete contracts={0}; savings=0" -f $capture.Records.Count)
    if ($DryRun) {
        [pscustomobject]@{ CaptureAt = $capture.CaptureAt.ToString("yyyy-MM-dd HH:mm:ss zzz"); ExpectedCardCount = $capture.ExpectedCardCount; ContractCount = $capture.Records.Count; SavingsCount = 0; Records = @($capture.Records | Select-Object Key, Symbol, Created, Product, Leverage, Investment, GridProfit) } | ConvertTo-Json -Depth 6
        exit 0
    }
    Update-Workbook $capture
    Write-RunLog "completed"
} catch {
    $detail = ($_ | Out-String).Trim()
    Write-RunLog ("FAILED: " + $detail)
    Write-Error $detail
    exit 1
}
