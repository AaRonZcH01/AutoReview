param([switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$workspace = $PSScriptRoot
Set-Location -LiteralPath $workspace
$python = Join-Path $workspace '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Build machine needs the project Python environment. Run Prepare-Environment.ps1 first.' }
if (-not $SkipFrontend) { & pnpm build; if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' } }
$lock=Get-Content -LiteralPath (Join-Path $workspace 'cli-distribution.lock.json') -Raw | ConvertFrom-Json
& $python scripts/fetch_codex.py
if ($LASTEXITCODE -ne 0) { throw 'Pinned public CLI download/check failed' }
$cli = Get-Item -LiteralPath (Join-Path $workspace 'resources/codex.exe')
$resources = Join-Path $workspace 'resources'
New-Item -ItemType Directory -Path $resources -Force | Out-Null
# Executables only. Never package CODEX_HOME, auth files, account profiles or user data.
foreach($entry in $lock.files){
    $sourceBinary=Join-Path $cli.DirectoryName $entry.name
    if((Get-FileHash -LiteralPath $sourceBinary -Algorithm SHA256).Hash -ne $entry.sha256){throw "CLI source checksum mismatch: $($entry.name)"}
    if((Split-Path -Parent $sourceBinary) -ne $resources){Copy-Item -LiteralPath $sourceBinary -Destination $resources -Force}
}
$cliVersion = & $cli.FullName --version
if($cliVersion -ne $lock.cliVersion){throw 'CLI version differs from the verified protocol pin. Review and update the distribution lock explicitly.'}
foreach($entry in $lock.files){
    $binary=Join-Path $resources $entry.name
    if(-not(Test-Path -LiteralPath $binary) -or (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash -ne $entry.sha256){throw "CLI checksum mismatch: $($entry.name)"}
}
& $python .delivery/check_cli_catalogue.py (Join-Path $resources 'codex.exe')
if($LASTEXITCODE -ne 0){throw 'Bundled CLI cannot represent default image models/reasoning. Distribution stopped.'}
$cliManifest = @{ cliVersion=$cliVersion; files=@(Get-ChildItem -LiteralPath $resources -Filter '*.exe' | ForEach-Object {
    @{name=$_.Name;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash;bytes=$_.Length}
}) }
$cliManifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $resources 'cli-manifest.json') -Encoding utf8
& $python -m PyInstaller --noconfirm --onedir --windowed --name AutoReview-Service --paths src/backend `
    --distpath release/runtime --workpath build/pyinstaller --specpath build `
    --add-data "${workspace}/dist;dist" --add-data "${workspace}/resources;resources" `
    --hidden-import tkinter --hidden-import tkinter.filedialog --collect-all openpyxl `
    --exclude-module torch --exclude-module tensorflow --collect-all cv2 --hidden-import websockets.legacy.server `
    --collect-all onnxruntime --collect-all rapidocr --exclude-module matplotlib `
    src/backend/desktop_entry.py
if ($LASTEXITCODE -ne 0) { throw 'Portable runtime build failed' }
$portable = Join-Path $workspace 'release/runtime/AutoReview-Service'
Copy-Item -LiteralPath (Join-Path $workspace 'PORTABLE-README.txt') -Destination $portable -Force
$runtimeDocs = Join-Path $portable 'docs'
New-Item -ItemType Directory -Path $runtimeDocs -Force | Out-Null
foreach ($documentName in @('USER_GUIDE.md', 'USER_GUIDE.docx')) {
    Copy-Item -LiteralPath (Join-Path $workspace $documentName) -Destination $runtimeDocs -Force
}
# Historical engineering documents contain private machine/account evidence.
foreach ($privateDoc in @('PRD.md', 'PRD.docx')) {
    $privatePath=Join-Path $runtimeDocs $privateDoc
    if(Test-Path -LiteralPath $privatePath){Remove-Item -LiteralPath $privatePath -Force}
}
foreach ($noticeName in @('LICENSE','THIRD_PARTY_NOTICES.md','PRIVACY.md')) {
    Copy-Item -LiteralPath (Join-Path $workspace $noticeName) -Destination $portable -Force
}
Copy-Item -LiteralPath (Join-Path $workspace 'third_party') -Destination $portable -Recurse -Force
& $python scripts/audit_public.py $portable
if ($LASTEXITCODE -ne 0) { throw 'Runtime privacy gate failed' }
Get-ChildItem -LiteralPath $portable -Recurse -File | ForEach-Object {
    @{path=$_.FullName.Substring($portable.Length+1);sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash;bytes=$_.Length}
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $portable 'distribution-manifest.json') -Encoding utf8
Write-Output "Runtime ready: $portable"
Write-Output 'Tauri desktop bundling is a separate Build-Desktop.ps1 step. This runtime can be checked with --no-browser --ready-file.'
& $python .delivery/package_runtime.py
if ($LASTEXITCODE -ne 0) { throw 'Runtime archive integrity verification failed' }
