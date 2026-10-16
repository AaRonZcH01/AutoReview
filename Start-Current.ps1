# Launch current source, never a frozen or packaged copy. No installer is produced.
param(
    [switch]$NoBrowser,
    [switch]$Browser,
    [string]$RuntimeRoot = (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies')
)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskRuntime=Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Force -Path $taskRuntime | Out-Null
$taskReady=Join-Path $taskRuntime 'current-source.json'
$taskLog=Join-Path $taskRuntime ('current-startup-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+$PID+'.log')
$taskTranscript=$false
$taskMutex=New-Object Threading.Mutex($false,'Local\AutoReviewSourceLauncher')
$taskLock=$false
$taskProgressFile=Join-Path $taskRuntime ('startup-progress-'+$PID+'.json')
function Set-StartupProgress([int]$percent,[string]$status,[bool]$done=$false){
    @{percent=$percent;status=$status;done=$done}|ConvertTo-Json|Set-Content -LiteralPath $taskProgressFile -Encoding UTF8
}
function Show-AlreadyRunning {
    if(-not $NoBrowser){Add-Type -AssemblyName System.Windows.Forms;[void][Windows.Forms.MessageBox]::Show('程序正在启动或已在运行，请勿重复运行！','AutoReview')}
    Write-Output '程序正在启动或已在运行，请勿重复运行！'
}
function Get-SourceFingerprint([string[]]$paths){
    $rows=foreach($path in $paths){Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot $path) -File -Recurse|Sort-Object FullName|ForEach-Object {$file= [IO.File]::OpenRead($_.FullName);$hash=[Security.Cryptography.SHA256]::Create();try{$_.FullName+':'+[BitConverter]::ToString($hash.ComputeHash($file)).Replace('-','')}finally{$file.Dispose();$hash.Dispose()}}}
    $sha=[Security.Cryptography.SHA256]::Create();try{[Convert]::ToBase64String($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($rows -join "`n"))))}finally{$sha.Dispose()}
}
$taskOriginalPath=$env:PATH
$taskOriginalPnpmPolicy=$env:pnpm_config_pm_on_fail
try {
    try{$taskLock=$taskMutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$taskLock=$true}
    if(-not $taskLock){Show-AlreadyRunning;return}
    Set-StartupProgress 5 '正在检查当前源码…'
    Start-Transcript -LiteralPath $taskLog -Force | Out-Null
    $taskTranscript=$true
    if(-not $NoBrowser -and -not $Browser){
        $taskShellPath=Join-Path $PSScriptRoot 'src-tauri/target/debug/autoreview-desktop.exe'
        $taskExistingShell=Get-Process -Name 'autoreview-desktop' -ErrorAction SilentlyContinue | Where-Object {$_.Path -eq $taskShellPath} | Select-Object -First 1
        if($taskExistingShell){
            Show-AlreadyRunning
            return
        }
    }
    if(Test-Path -LiteralPath $taskReady){
        $taskExisting=Get-Content -LiteralPath $taskReady -Raw | ConvertFrom-Json
        $taskProcess=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$taskExisting.pid) -ErrorAction SilentlyContinue
        if($taskProcess -and $taskProcess.CommandLine -match 'autoreview.v2.launch' -and $taskProcess.CommandLine.Contains($taskReady)){
            if(-not $NoBrowser){Start-Process -FilePath $taskExisting.url}
            Write-Output '当前源码程序已在运行，已打开其页面。更新源码后请先关闭原页面再重新启动。'
            return
        }
    }
    if(-not $NoBrowser){Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'Start-Progress.ps1')+'" -ProgressFile "'+$taskProgressFile+'" -Owner '+$PID)|Out-Null}
    $taskPython=Join-Path $PSScriptRoot '.venv/Scripts/pythonw.exe'
    if(-not(Test-Path -LiteralPath $taskPython)){throw '缺少项目开发环境，请先运行 Prepare-Environment.ps1。'}
    # Explorer does not inherit Codex's injected PATH. Resolve the same runtime
    # used by Prepare-Environment.ps1 and invoke pnpm through its Node executable.
    $taskNode=Join-Path $RuntimeRoot 'node/bin/node.exe'
    $taskPnpm=Join-Path $RuntimeRoot 'node/node_modules/pnpm/bin/pnpm.mjs'
    $taskBundled=(Test-Path -LiteralPath $taskNode) -and (Test-Path -LiteralPath $taskPnpm)
    if(-not $taskBundled){
        $taskNodeCommand=Get-Command node.exe -ErrorAction SilentlyContinue
        $taskPnpmCommand=Get-Command pnpm.cmd -ErrorAction SilentlyContinue
        if(-not $taskNodeCommand -or -not $taskPnpmCommand){
            throw '找不到已准备的 Node/pnpm 运行时。请运行 Prepare-Environment.ps1；若依赖位置已迁移，可为 Start-Current.ps1 指定 -RuntimeRoot。'
        }
        $taskNode=$taskNodeCommand.Source
        $taskPnpm=$taskPnpmCommand.Source
    }
    $env:PATH=(Split-Path -Parent $taskNode)+[IO.Path]::PathSeparator+$taskOriginalPath
    $env:pnpm_config_pm_on_fail='ignore'
    $taskNodeMajor=& $taskNode -p 'process.versions.node.match(/^[0-9]+/)[0]'
    if($LASTEXITCODE -ne 0 -or $taskNodeMajor -ne '24'){throw '当前开发依赖需要 Node 24，请使用 Prepare-Environment.ps1 对应的运行时。'}
    $taskFrontendStamp=Get-SourceFingerprint @('src/frontend','public','package.json','pnpm-lock.yaml','vite.config.ts')
    $taskStampFile=Join-Path $taskRuntime 'frontend-fingerprint.txt'
    if(-not(Test-Path -LiteralPath 'dist/index.html') -or -not(Test-Path -LiteralPath $taskStampFile) -or (Get-Content -LiteralPath $taskStampFile -Raw).Trim() -ne $taskFrontendStamp){
        Set-StartupProgress 20 '正在更新当前界面…'
        if($taskBundled){& $taskNode $taskPnpm build}else{& $taskPnpm build}
        if($LASTEXITCODE -ne 0){throw '前端构建失败，未启动旧版界面。'}
        Set-Content -LiteralPath $taskStampFile -Value $taskFrontendStamp
    }
    if(-not $NoBrowser -and -not $Browser){
        $taskCargo=Join-Path $env:USERPROFILE '.cargo/bin/cargo.exe'
        if(-not(Test-Path -LiteralPath $taskCargo)){throw '缺少已配置的 Rust 开发环境。可用 -Browser 启动辅助浏览器模式。'}
        $taskNativeStamp=Get-SourceFingerprint @('src-tauri/src','src-tauri/capabilities','src-tauri/permissions','src-tauri/Cargo.toml','src-tauri/tauri.conf.json','src-tauri/build.rs','src-tauri/icons','dist')
        $taskNativeStampFile=Join-Path $taskRuntime 'native-fingerprint.txt'
        if(-not(Test-Path -LiteralPath $taskShellPath) -or -not(Test-Path -LiteralPath $taskNativeStampFile) -or (Get-Content -LiteralPath $taskNativeStampFile -Raw).Trim() -ne $taskNativeStamp){
            Set-StartupProgress 55 '正在构建源码桌面窗口（首次或更新后需要等待）…'
            & $taskCargo build --manifest-path (Join-Path $PSScriptRoot 'src-tauri/Cargo.toml')
            if($LASTEXITCODE -ne 0){throw '源码桌面窗口构建失败。'}
            Set-Content -LiteralPath $taskNativeStampFile -Value $taskNativeStamp
        }
        Set-StartupProgress 90 '正在打开桌面启动进度界面…'
        $env:AUTOREVIEW_SOURCE_ROOT=$PSScriptRoot
        $taskShell=Join-Path $PSScriptRoot 'src-tauri/target/debug/autoreview-desktop.exe'
        Start-Process -FilePath $taskShell -WindowStyle Hidden -WorkingDirectory $PSScriptRoot
        Write-Output '已启动 v1.4.2 源码桌面窗口；后台由桌面窗口管理，可正常最小化。'
        return
    }
    Set-StartupProgress 85 '正在启动本地服务…'
    $env:PYTHONPATH=Join-Path $PSScriptRoot 'src/backend'
    if(Test-Path -LiteralPath $taskReady){Remove-Item -LiteralPath $taskReady}
    $taskArguments='-m autoreview.v2.launch --no-browser --ready-file "'+$taskReady+'"'
    $taskChild=Start-Process -FilePath $taskPython -ArgumentList $taskArguments -WindowStyle Hidden -WorkingDirectory $PSScriptRoot -PassThru
    $taskDeadline=(Get-Date).AddSeconds(35)
    while(-not(Test-Path -LiteralPath $taskReady) -and -not $taskChild.HasExited -and (Get-Date) -lt $taskDeadline){Start-Sleep -Milliseconds 200;$taskChild.Refresh()}
    if(-not(Test-Path -LiteralPath $taskReady)){throw '后台未能启动。请先关闭旧版 AutoReview 页面并等待退出；具体原因见本机 AutoReview/startup-logs/service.log。'}
    $taskConnection=Get-Content -LiteralPath $taskReady -Raw | ConvertFrom-Json
    if(-not $NoBrowser){Start-Process -FilePath $taskConnection.url}
    Write-Output 'AutoReview v1.3.0 辅助浏览器源码已启动，关闭全部页面后后台自动退出。'
} catch {
    $taskError=$_.Exception.Message
    Write-Output $taskError
    if(-not $NoBrowser){
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show($taskError+"`n启动日志："+$taskLog,'AutoReview 启动失败')
    }
    throw
} finally {
    Set-StartupProgress 100 '启动阶段完成' $true
    if($taskLock){$taskMutex.ReleaseMutex()};$taskMutex.Dispose()
    $env:PATH=$taskOriginalPath
    $env:pnpm_config_pm_on_fail=$taskOriginalPnpmPolicy
    if($taskTranscript){Stop-Transcript | Out-Null}
}


