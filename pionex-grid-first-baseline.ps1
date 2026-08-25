param(
    [datetime]$BaselineDate = [datetime]::Now.Date,
    [string]$RootDir = "D:\My-project\pionex grid record"
)

$main = Join-Path ([System.IO.Path]::GetFullPath($RootDir)) "pionex-grid-daily.ps1"
& $main -ReplaceDate $BaselineDate -RootDir $RootDir
exit $LASTEXITCODE
