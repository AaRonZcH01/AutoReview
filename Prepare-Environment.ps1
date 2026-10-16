param([string]$PythonPath = 'python', [switch]$CheckOnly)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
function Invoke-Checked([string]$program, [string[]]$arguments) {
    & $program @arguments
    if($LASTEXITCODE -ne 0){throw "Dependency command failed: $program"}
}
Invoke-Checked $PythonPath @('-c','import sys; assert sys.version_info[:2] == (3,12), "Python 3.12 required"')
Invoke-Checked 'node' @('-e','if(process.versions.node.split(".")[0] !== "24") throw Error("Node 24 required")')
$python=Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if(-not $CheckOnly){
    if(-not(Test-Path -LiteralPath $python)){Invoke-Checked $PythonPath @('-m','venv','.venv')}
    Invoke-Checked $python @('-m','pip','install','--index-url','https://pypi.org/simple','--disable-pip-version-check','-r','requirements.lock.txt')
    Invoke-Checked 'pnpm' @('install','--frozen-lockfile')
}
if(-not(Test-Path -LiteralPath $python)){throw 'Run Prepare-Environment.ps1 before CheckOnly'}
Invoke-Checked $python @('-m','pip','check')
Invoke-Checked $python @('scripts/check_environment.py')
