# 注册 Windows 计划任务：周一至周五 07:05 开始，每小时刷新一次，共 8 次（到 15:05）
#
# 安装：
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
# 卸载：
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove

param(
    [string]$TaskName = "MarketMainlineDashboard",
    [string]$StartTime = "07:05",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已卸载计划任务：$TaskName"
    } else {
        Write-Host "计划任务不存在：$TaskName"
    }
    exit 0
}

$Root = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $Root "scripts\refresh.ps1"
if (-not (Test-Path $ScriptPath)) {
    throw "找不到刷新脚本：$ScriptPath"
}

$At = [datetime]::ParseExact($StartTime, "HH:mm", $null)

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
    -WorkingDirectory $Root

# 每周一至周五触发一次，随后每小时重复，持续 8.5 小时（约 07:05–15:05）
$Trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $At
$Repetition = (New-ScheduledTaskTrigger -Once -At $At `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 8 -Minutes 30)).Repetition
$Trigger.Repetition = $Repetition

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Interactive：仅在当前用户登录时运行，便于访问 Git 凭据和用户环境
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "交易时段每小时刷新市场主线看板并推送到 GitHub Pages" `
    -Force | Out-Null

Write-Host "已注册计划任务：$TaskName"
Write-Host "运行时间：周一至周五 $StartTime 起，每小时一次，至 15:05"
Write-Host "日志目录：$(Join-Path $Root 'logs')"
Write-Host "手动测试：Start-ScheduledTask -TaskName $TaskName"
