# 交易时段刷新：采集 -> 生成静态页面 -> 提交并推送 GitHub
# 由 Windows 计划任务调用，也可以手动执行：
#   powershell -ExecutionPolicy Bypass -File scripts\refresh.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\refresh.ps1 -SkipPush   # 只采集+导出，不提交不推送

param(
    # 只跑采集与页面导出，便于本地验证；不执行 git add/commit/push
    [switch]$SkipPush
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir ("refresh_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 日志只保留最近 30 天，避免每天 9 次刷新把磁盘堆满
Get-ChildItem -Path $LogDir -Filter "refresh_*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$StartedAt = Get-Date

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

# 用 Start-Process 重定向，避免 PowerShell 把 Python 的 stderr 日志当成
# NativeCommandError（在 ErrorActionPreference=Stop 下会直接中断脚本）。
function Invoke-PythonStep {
    param(
        [string]$Title,
        [string[]]$Arguments,
        [string]$PythonPath,
        [string]$WorkingDir,
        [string]$LogPath,
        [string]$TempDir
    )
    Write-Log "==> $Title"
    $stamp = Get-Date -Format "HHmmssfff"
    $outFile = Join-Path $TempDir "step_$stamp.out"
    $errFile = Join-Path $TempDir "step_$stamp.err"
    try {
        $process = Start-Process -FilePath $PythonPath -ArgumentList $Arguments `
            -WorkingDirectory $WorkingDir -Wait -PassThru -NoNewWindow `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        # 注意不能用 Tee-Object：PS 5.1 下它按 UTF-16LE 写文件，
        # 与 Write-Log 的 UTF-8 混在一个日志里会变成乱码，统一用 Add-Content -Encoding UTF8
        foreach ($file in @($outFile, $errFile)) {
            if (-not (Test-Path $file)) { continue }
            $lines = @(Get-Content $file -Encoding UTF8)
            if ($lines.Count -eq 0) { continue }
            Add-Content -Path $LogPath -Value $lines -Encoding UTF8
            $lines | ForEach-Object { Write-Host $_ }
        }
        if ($process.ExitCode -ne 0) {
            throw "$Title 失败（退出码 $($process.ExitCode)）"
        }
    }
    finally {
        Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
    }
}

# git 的正常进度也走 stderr；局部改为 Continue，避免被 Stop 误判为失败。
function Invoke-Git {
    param([string[]]$Arguments, [string]$LogPath)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git @Arguments 2>&1
        $script:LastGitExit = $LASTEXITCODE
        $lines = @($output | ForEach-Object { $_.ToString() })
        if ($lines.Count -gt 0) {
            Add-Content -Path $LogPath -Value $lines -Encoding UTF8
            $lines | ForEach-Object { Write-Host $_ }
        }
        return $output
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

Push-Location $Root
try {
    # 强制 UTF-8，避免中文日志乱码
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:TQDM_DISABLE = "1"
    $env:GIT_TERMINAL_PROMPT = "0"
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    $Python = if (Test-Path $VenvPython) {
        $VenvPython
    } else {
        (Get-Command python -ErrorAction Stop).Source
    }

    Write-Log "开始刷新：$Root"

    if (-not (Test-Path (Join-Path $Root ".git"))) {
        throw "当前目录不是 Git 仓库，请先按 README 完成 git init / remote 配置"
    }

    $Remote = (Invoke-Git -Arguments @("remote", "get-url", "origin") -LogPath $LogFile) -join ""
    if ($script:LastGitExit -ne 0 -or -not $Remote.Trim()) {
        throw "未配置 origin 远程仓库，无法推送"
    }

    Invoke-PythonStep -Title "采集与落库" -Arguments @("run_daily.py", "--intraday") `
        -PythonPath $Python -WorkingDir $Root -LogPath $LogFile -TempDir $LogDir

    Invoke-PythonStep -Title "生成静态页面" -Arguments @("publish.py") `
        -PythonPath $Python -WorkingDir $Root -LogPath $LogFile -TempDir $LogDir

    if ($SkipPush) {
        $elapsed = [int]((Get-Date) - $StartedAt).TotalSeconds
        Write-Log "已指定 -SkipPush：跳过提交与推送（耗时 $elapsed 秒）"
        exit 0
    }

    Write-Log "==> 提交并推送 docs/"
    Invoke-Git -Arguments @("add", "docs") -LogPath $LogFile | Out-Null
    if ($script:LastGitExit -ne 0) {
        throw "git add 失败"
    }

    Invoke-Git -Arguments @("diff", "--cached", "--quiet") -LogPath $LogFile | Out-Null
    if ($script:LastGitExit -eq 0) {
        Write-Log "页面内容无变化，跳过提交"
        exit 0
    }

    $Message = "refresh: {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm")
    Invoke-Git -Arguments @("commit", "-m", $Message) -LogPath $LogFile | Out-Null
    if ($script:LastGitExit -ne 0) {
        throw "git commit 失败"
    }

    # 网络抖动（Empty reply / 连接超时）很常见，推送按「重试 → rebase 再重试」处理。
    function Invoke-Push([int]$Attempts = 3, [int]$DelaySeconds = 10) {
        for ($i = 1; $i -le $Attempts; $i++) {
            Invoke-Git -Arguments @("push", "-u", "origin", "HEAD") -LogPath $LogFile | Out-Null
            if ($script:LastGitExit -eq 0) {
                return $true
            }
            if ($i -lt $Attempts) {
                Write-Log "git push 第 $i 次失败，$DelaySeconds 秒后重试"
                Start-Sleep -Seconds $DelaySeconds
            }
        }
        return $false
    }

    $Pushed = Invoke-Push
    if (-not $Pushed) {
        Write-Log "推送持续失败，尝试 rebase 后再推"
        Invoke-Git -Arguments @("pull", "--rebase", "--autostash") -LogPath $LogFile | Out-Null
        if ($script:LastGitExit -ne 0) {
            throw "git pull --rebase 失败，请手动处理后重试"
        }
        if (-not (Invoke-Push -Attempts 2)) {
            throw "git push 失败（已重试），请检查网络、凭证或远端状态"
        }
    }

    $elapsed = [int]((Get-Date) - $StartedAt).TotalSeconds
    Write-Log "刷新完成并已推送（耗时 $elapsed 秒）"
    exit 0
}
catch {
    $message = $_.Exception.Message
    if (-not $message) { $message = ($_ | Out-String).Trim() }
    $elapsed = [int]((Get-Date) - $StartedAt).TotalSeconds
    Write-Log "刷新失败（耗时 $elapsed 秒）：$message"
    exit 1
}
finally {
    Pop-Location
}
