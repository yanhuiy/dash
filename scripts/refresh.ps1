# 交易时段刷新：采集 -> 生成静态页面 -> 提交并推送 GitHub
# 由 Windows 计划任务调用，也可以手动执行：
#   powershell -ExecutionPolicy Bypass -File scripts\refresh.ps1

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir ("refresh_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Invoke-Step {
    param([string]$Title, [scriptblock]$Command)
    Write-Log "==> $Title"
    & $Command 2>&1 | Tee-Object -FilePath $LogFile -Append
    if ($LASTEXITCODE -ne 0) {
        throw "$Title 失败（退出码 $LASTEXITCODE）"
    }
}

Push-Location $Root
try {
    # 强制 UTF-8，避免中文日志乱码
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
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

    $Remote = git remote get-url origin 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $Remote) {
        throw "未配置 origin 远程仓库，无法推送"
    }

    Invoke-Step "采集与落库" { & $Python run_daily.py --intraday }
    Invoke-Step "生成静态页面" { & $Python publish.py }

    Write-Log "==> 提交并推送 docs/"
    git add docs
    if ($LASTEXITCODE -ne 0) {
        throw "git add 失败"
    }

    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Log "页面内容无变化，跳过提交"
        exit 0
    }

    $Message = "refresh: {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm")
    git commit -m $Message
    if ($LASTEXITCODE -ne 0) {
        throw "git commit 失败"
    }

    git push
    if ($LASTEXITCODE -ne 0) {
        Write-Log "远端有新提交，尝试 rebase 后重试推送"
        git pull --rebase --autostash
        if ($LASTEXITCODE -ne 0) {
            throw "git pull --rebase 失败，请手动处理后重试"
        }
        git push
        if ($LASTEXITCODE -ne 0) {
            throw "git push 失败，请检查凭证或网络"
        }
    }

    Write-Log "刷新完成并已推送"
    exit 0
}
catch {
    Write-Log "刷新失败：$($_.Exception.Message)"
    exit 1
}
finally {
    Pop-Location
}
