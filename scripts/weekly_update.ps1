# Run by Windows Task Scheduler (python -m paper_finder schedule install).
# Updates every topic, writes reports\<date>_<topic>.html, shows a toast, logs to logs\update_<date>.log.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force (Join-Path $root 'logs') | Out-Null
$stamp = '{0:yyyy-MM-dd_HHmm}' -f (Get-Date)
$env:PYTHONIOENCODING = 'utf-8'
$p = Start-Process -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
    -ArgumentList '-m', 'paper_finder', 'update', '--trigger', 'schedule', '--notify' `
    -WorkingDirectory $root -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput (Join-Path $root "logs\update_$stamp.log") `
    -RedirectStandardError (Join-Path $root "logs\update_$stamp.err.log")
exit $p.ExitCode
