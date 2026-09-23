param(
    [string]$SnapshotJson = "",
    [datetime]$CaptureAt = [datetime]::MinValue,
    [string]$RootDir = "D:\My-project\pionex grid record"
)

$main = Join-Path ([System.IO.Path]::GetFullPath($RootDir)) "pionex-grid-daily.ps1"
if ($CaptureAt -eq [datetime]::MinValue) {
    & $main -Backfill -SnapshotJson $SnapshotJson -RootDir $RootDir
} else {
    & $main -Backfill -SnapshotJson $SnapshotJson -CaptureAt $CaptureAt -RootDir $RootDir
}
exit $LASTEXITCODE
