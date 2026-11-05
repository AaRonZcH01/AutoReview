param([switch]$SkipRuntime)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
if(-not $SkipRuntime){ & (Join-Path $PSScriptRoot 'Build-Portable.ps1'); if($LASTEXITCODE -ne 0){throw 'Runtime build failed'} }
$cargoCommand=Get-Command cargo.exe -ErrorAction SilentlyContinue
$cargo=if($cargoCommand){$cargoCommand.Source}else{Join-Path $env:USERPROFILE '.cargo/bin/cargo.exe'}
$vswhere=Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if(-not(Test-Path -LiteralPath $vswhere)){throw 'Windows C++ Build Tools is required on the build machine only.'}
$vs=& $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if(-not $vs){throw 'C++ workload missing. Complete the Windows administrator installation prompt first.'}
$taskPathBefore=$env:PATH
$taskRustFlagsBefore=$env:CARGO_ENCODED_RUSTFLAGS
try {
    $env:PATH="$(Split-Path -Parent $cargo);$taskPathBefore"
    # Keep local build paths out of release panic strings and macro locations.
    # Encoded flags preserve Windows user paths containing spaces.
    $taskRemap=@()
    foreach($taskPrefix in @($env:USERPROFILE,$PSScriptRoot)) {
        $taskAlias=if($taskPrefix -eq $PSScriptRoot){'autoreview'}else{'build-user'}
        $taskRemap+=('--remap-path-prefix='+$taskPrefix+'='+$taskAlias)
        $taskRemap+=('--remap-path-prefix='+$taskPrefix.Replace('\','/')+'='+$taskAlias)
    }
    $env:CARGO_ENCODED_RUSTFLAGS=(@($taskRustFlagsBefore)+$taskRemap | Where-Object {$_}) -join [char]31
    & pnpm exec tauri build -- --locked
    if($LASTEXITCODE -ne 0){throw 'Desktop bundle failed'}
    & (Join-Path $PSScriptRoot '.venv/Scripts/python.exe') .delivery/package_desktop.py
    if($LASTEXITCODE -ne 0){throw 'Desktop distribution verification failed'}
} finally { $env:PATH=$taskPathBefore; $env:CARGO_ENCODED_RUSTFLAGS=$taskRustFlagsBefore }
