# 注册 Windows 计划任务：周一至周五按固定时点各刷新一次
# 默认 9 个时点：07:00 09:00 11:00 12:00 13:00 15:00 18:00 20:00 22:00
#
# 安装：
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Times "07:00,09:00,11:00,12:00,13:00,15:00,18:00,20:00,22:00"
# 卸载：
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove

param(
    [string]$TaskName = "MarketMainlineDashboard",
    [string]$Times = "07:00,09:00,11:00,12:00,13:00,15:00,18:00,20:00,22:00",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

# 注册计划任务需要管理员权限；非提升状态时自动请求 UAC
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "需要管理员权限，正在请求提升……"
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($Remove) { $arguments += "-Remove" }
    if ($TaskName -ne "MarketMainlineDashboard") {
        $arguments += @("-TaskName", "`"$TaskName`"")
    }
    if ($Times -ne "07:00,09:00,11:00,12:00,13:00,15:00,18:00,20:00,22:00") {
        $arguments += @("-Times", "`"$Times`"")
    }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arguments
    exit 0
}

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

$TimeList = @($Times.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($TimeList.Count -eq 0) {
    throw "-Times 不能为空，示例：-Times ""09:00,11:00,15:00"""
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
    -WorkingDirectory $Root

# 每个时点一个独立触发器：周一到周五的该时刻各触发一次
$Triggers = @()
foreach ($item in $TimeList) {
    $at = [datetime]::ParseExact($item, "HH:mm", $null)
    $Triggers += New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
        -At $at
}

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
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
    -Trigger $Triggers `
    -Settings $Settings `
    -Principal $Principal `
    -Description "交易时段与盘后定时刷新市场主线看板并推送到 GitHub Pages" `
    -Force | Out-Null

Write-Host "已注册计划任务：$TaskName"
Write-Host "运行时间：周一至周五 $($TimeList -join ' / ')（共 $($TimeList.Count) 次）"
Write-Host "日志目录：$(Join-Path $Root 'logs')"
Write-Host "手动测试：Start-ScheduledTask -TaskName $TaskName"

# 回读注册结果，确认触发器与实例策略生效
$Registered = Get-ScheduledTask -TaskName $TaskName
$RegisteredTimes = @($Registered.Triggers | ForEach-Object {
        ([datetime]$_.StartBoundary).ToString("HH:mm")
    })
Write-Host ""
Write-Host "触发器检查：$($RegisteredTimes -join ' / ')（共 $($RegisteredTimes.Count) 个）"
Write-Host "实例策略：$($Registered.Settings.MultipleInstances)  超时=$($Registered.Settings.ExecutionTimeLimit)  失败重启=$($Registered.Settings.RestartCount)次"
$Info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "状态：$($Registered.State)  上次运行=$($Info.LastRunTime)  上次结果=$($Info.LastTaskResult)"
