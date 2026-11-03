# 市场主线切换看板

盘后/盘中采集公开信息，按主线归档到 SQLite，并导出静态页面到 GitHub Pages。
本地仍保留 Streamlit 单页看板，方便调试。

## 目录

| 路径 | 作用 |
| --- | --- |
| `run_daily.py` | 运行一次采集与落库（`--intraday` 为盘中/盘前模式） |
| `publish.py` | 把 SQLite 快照导出到 `docs/`（最新页 + 历史归档页） |
| `app.py` | Streamlit 本地看板 |
| `scripts/refresh.ps1` | 刷新 + 导出 + git 提交推送（计划任务实际调用的脚本） |
| `scripts/install_task.ps1` | 注册/卸载 Windows 计划任务 |
| `data/market.db` | 本地 SQLite 数据库（已加入 `.gitignore`） |
| `docs/index.html` | 最新快照页面（GitHub Pages 入口） |
| `docs/date/*.html` | 按日期归档的历史页面 |

## 一、本地运行

```powershell
python -m pip install -r requirements.txt

# 采集并落库（非交易日自动跳过）
python run_daily.py --intraday

# 生成静态页面
python publish.py

# 本地 Streamlit 看板（可选）
streamlit run app.py
```

`--intraday` 会让盘前/盘中运行时仍按“今天”生成快照，避免把上一交易日的归档覆盖掉。
同一日期多次运行会覆盖当日快照，属于设计行为。

## 二、推送到 GitHub

### 1. 初始化仓库并首次推送

```powershell
git init -b main
git add .
git commit -m "init: market mainline dashboard"
git remote add origin git@github.com:<你的用户名>/<仓库名>.git
git push -u origin main
```

使用 HTTPS 也可以，但需要先手动完成一次登录，让 Git Credential Manager 缓存凭据：

```powershell
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

计划任务运行时无法交互输入密码，`refresh.ps1` 已设置 `GIT_TERMINAL_PROMPT=0`，
凭据缺失时会直接失败并写入日志。

如果网络封掉了 `github.com:443`（报 `Recv failure: Connection was reset`），
可以改走 SSH 的 443 端口（`ssh.github.com:443`）：

```powershell
# 1. 生成无口令密钥（计划任务需要无人值守推送）
ssh-keygen -t ed25519 -C "dashboard" -f "$env:USERPROFILE\.ssh\id_ed25519"
#    提示 passphrase 时直接回车两次

# 2. 把 id_ed25519.pub 整行内容加到 GitHub → Settings → SSH and GPG keys（Authentication Key）

# 3. 记录主机指纹，然后改用 443 端口的 SSH 地址
ssh-keyscan -p 443 ssh.github.com >> "$env:USERPROFILE\.ssh\known_hosts"
git remote set-url origin ssh://git@ssh.github.com:443/<你的用户名>/<仓库名>.git
git push -u origin main
```

### 2. 开启 GitHub Pages

仓库 → **Settings** → **Pages**：

- Source：**Deploy from a branch**
- Branch：**main**，目录选择 **/docs**
- 保存后访问：`https://<你的用户名>.github.io/<仓库名>/`

每次计划任务推送后，Pages 会自动重新发布，通常 1–2 分钟内生效。
最新页每 10 分钟自动重载一次，因此打开页面后无需手动刷新太久。

### 3. 隐私提示

- **公开仓库**：`holdings.yaml` 本身会随仓库公开，页面 D 区块中的持仓也会被所有人看到。
  当前 `config.yaml` 已设置 `publish.include_holdings: false`（静态页不渲染 D 区块，
  本地 Streamlit 看板仍显示）；如需彻底隐藏，还应清空或替换 `holdings.yaml` 中的真实持仓。
- **私有仓库**：GitHub Pages 需要付费套餐（Pro/Team/Enterprise），且站点默认仍是公开可访问。
- 页面已添加 `<meta name="robots" content="noindex">`，但不要把它当成访问控制。
- 如果持仓敏感，建议只在本地使用 Streamlit 看板，或部署到需要登录的托管平台。

## 三、Windows 计划任务（07:05–15:05 每小时）

安装（脚本会自动请求管理员权限，UAC 弹窗选择“是”）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
```

规则：

- 周一至周五 **07:05** 首次运行，之后每小时一次，到 **15:05**（共 9 次）
- 仅在当前用户登录时运行（便于读取 Git 凭据）
- 同一时间只允许一个实例；单次最长 30 分钟
- 非交易日由 `run_daily.py` 自动跳过

手动测试：

```powershell
Start-ScheduledTask -TaskName MarketMainlineDashboard
# 或直接运行
powershell -ExecutionPolicy Bypass -File scripts\refresh.ps1
```

查看日志：

```powershell
Get-Content logs\refresh_$(Get-Date -Format yyyyMMdd).log -Tail 40
```

卸载：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove
```

### 限制与注意事项

- 电脑必须开机且当前用户已登录，任务才会执行；关机/休眠期间不会补跑数据。
- 断网、数据源限流或 GitHub 推送失败时，本次刷新失败，日志中会记录原因；
  下一次整点任务会继续尝试。GitHub Pages 始终保留最后一次成功推送的版本。
- 数据源单次采集约需数分钟，9 次/天属于低频访问；若遇到限流，可在计划任务中减少运行次数。
- 07:05–09:05 的盘前快照可能沿用上一交易日收盘行情，页面“生成时间”和“缺失/降级信源”会如实标注。
- 每次提交只包含 `docs/` 文本页面，不提交 SQLite 数据库和日志，仓库不会因二进制文件快速膨胀。
- 如果希望保留数据库历史，可自行把 `data/` 从 `.gitignore` 移除，但不建议每小时提交二进制文件。

## 四、常用命令

```powershell
python run_daily.py --date 2026-11-02   # 手动补跑/重跑指定日期
python run_daily.py --json              # 输出 JSON 摘要
python publish.py --date 2026-11-02     # 只重新导出某个日期页面
```

## 五、免责声明

本看板仅用于研究与信息整理，不构成任何投资建议，不对涨跌做预测。
政策与研报只保留公开链接，不抓取付费全文。
