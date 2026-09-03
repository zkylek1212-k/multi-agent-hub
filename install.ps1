<#
.SYNOPSIS
    Multi-Agent ADE 派工中心 — 一鍵部署（Windows / PowerShell 5.1+）

.DESCRIPTION
    偵測本機可用的 Worker CLI、安裝 Python 相依、產生機器專屬的 .mcp.json，
    並做一次真正的匯入煙霧測試。可重複執行（idempotent）。

.PARAMETER SkipDeps
    跳過 pip install（已經裝過 mcp[cli] 時用）。

.PARAMETER Force
    覆蓋既有的 CLAUDE.md。

.PARAMETER DepsOnly
    只做「裝相依 + 偵測 Worker + 自我測試」，不碰 git / .gitignore / CLAUDE.md / .mcp.json。
    以 Claude Code plugin 安裝時用這個：plugin 目錄不是 git repo，也已自帶 .mcp.json 與 skill，
    不加這個旗標會在 plugin 快取裡 git init，並多註冊一份 local scope 的 agent-hub（重複載入）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1

.NOTES
    本檔必須以 UTF-8 with BOM 儲存，否則 PowerShell 5.1 會用系統 ANSI 讀取，
    中文註解會亂碼並讓字串括號配對失敗。
#>
[CmdletBinding()]
param(
    [switch]$SkipDeps,
    [switch]$Force,
    [switch]$DepsOnly
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function OK($msg)       { Write-Host "    OK   $msg" -ForegroundColor Green }
function Warn($msg)     { Write-Host "    WARN $msg" -ForegroundColor Yellow }
function Die($msg)      { Write-Host "`nFAIL $msg" -ForegroundColor Red; exit 1 }

function Invoke-Native {
    # PS 5.1 在 $ErrorActionPreference='Stop' 下，會把原生指令寫到 stderr 的每一行
    # 包成 NativeCommandError 終止錯誤 —— 即使該指令回傳 0。git 與 python 都會
    # 正常往 stderr 寫東西，所以一律透過這裡呼叫，並改看 exit code。
    param([scriptblock]$Block)
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    $out = & $Block 2>&1 | Out-String
    return [pscustomobject]@{ Code = $LASTEXITCODE; Out = $out }
}

Write-Host "Multi-Agent ADE 派工中心 — 部署" -ForegroundColor White
Write-Host "專案目錄: $root"

# --- 1. Python 3.10+ ---------------------------------------------------
Step 1 "尋找 Python 3.10 以上（hub 用到 str | None 語法，3.10 起才支援）"

$pyExe = $null
$pyArgs = @()
foreach ($c in @(
    @{ exe = 'py';      args = @('-3') },
    @{ exe = 'python';  args = @() },
    @{ exe = 'python3'; args = @() }
)) {
    if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
    $probe = Invoke-Native { & $c.exe @($c.args) -c "import sys;print(sys.version_info[0],sys.version_info[1])" }
    if ($probe.Code -ne 0) { continue }
    $nums = $probe.Out.Trim().Split(' ')
    if ($nums.Count -lt 2) { continue }
    $maj = [int]$nums[0]; $min = [int]$nums[1]
    if ($maj -ge 3 -and $min -ge 10) {
        $pyExe = $c.exe; $pyArgs = $c.args
        OK "$($c.exe) $($c.args -join ' ') -> Python $maj.$min"
        break
    }
    Warn "$($c.exe) 是 Python $maj.$min，低於 3.10，繼續找"
}
if (-not $pyExe) { Die "找不到 Python 3.10+。請安裝 https://www.python.org/downloads/ 並勾選 Add to PATH。" }

# --- 2. 相依套件 -------------------------------------------------------
Step 2 "安裝 Python 相依 (mcp[cli])"
if ($SkipDeps) {
    Warn "已指定 -SkipDeps，略過"
} else {
    $r = Invoke-Native { & $pyExe @pyArgs -m pip install --quiet --no-warn-script-location --upgrade "mcp[cli]" }
    if ($r.Code -ne 0) {
        Write-Host $r.Out
        Die "pip install 失敗。若是權限問題，改跑：$pyExe $($pyArgs -join ' ') -m pip install --user `"mcp[cli]`""
    }
    OK "mcp[cli] 已安裝"
}

# --- 3. 偵測 Worker ----------------------------------------------------
Step 3 "偵測可用的 Worker CLI"

# 已知的真實 .exe 路徑，優先於 PATH：npm 裝出來的是 .cmd shim，
# 走 cmd.exe 會竄改參數並有 8191 字上限（見架構文件 §6.2 / §6.3）。
$hints = @{
    claude_cli = @(
        "$env:USERPROFILE\.local\bin\claude.exe",
        "$env:APPDATA\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
    )
    agy_cli    = @("$env:LOCALAPPDATA\agy\bin\agy.exe")
    codex_cli  = @("$env:USERPROFILE\.codex\bin\codex.exe")
    ollama  = @()
}
$onPath = @{ claude_cli = 'claude'; agy_cli = 'agy'; codex_cli = 'codex'; ollama = 'ollama' }

$active = @()
$bins   = @{}
foreach ($w in @('claude_cli', 'agy_cli', 'codex_cli', 'ollama')) {
    $found = $null
    foreach ($h in $hints[$w]) {
        if ($h -and (Test-Path -LiteralPath $h)) { $found = (Resolve-Path -LiteralPath $h).Path; break }
    }
    if ($found) {
        $active += $w
        $bins["HUB_BIN_$($w.ToUpper())"] = ($found -replace '\\', '/')
        OK "$w -> $found"
        continue
    }
    $cmd = Get-Command $onPath[$w] -ErrorAction SilentlyContinue
    if (-not $cmd) { Warn "$w 未安裝（$($onPath[$w]) 不在 PATH），本次停用"; continue }
    $src = $cmd.Source
    $active += $w
    if ($src -match '\.(cmd|bat)$') {
        Warn "$w -> $src 是批次檔。prompt 走檔案傳遞故無注入風險，但建議手動指定 HUB_BIN_$($w.ToUpper())"
    } else {
        $bins["HUB_BIN_$($w.ToUpper())"] = ($src -replace '\\', '/')
        OK "$w -> $src"
    }
}
if ($active.Count -eq 0) { Die "一個 Worker 都沒偵測到。至少要裝一個，見 INSTALL.md 的工具清單。" }

$c_git = Get-Command git -ErrorAction SilentlyContinue
if ($c_git) { OK "git -> $($c_git.Source)" } else { Warn "git 未安裝" }
if (-not $c_git) {
    Die "git 是必要條件（worktree 隔離靠它）。https://git-scm.com/download/win"
}

$c_docker = Get-Command docker -ErrorAction SilentlyContinue
if ($c_docker) {
    OK "docker -> $($c_docker.Source)"
    $hasDocker = $true
} else {
    Warn "docker 未安裝，準備透過 winget 自動安裝 Docker Desktop..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        # 直接執行以顯示進度條與處理 UAC 提權提示。加上 --interactive 讓安裝畫面強制顯示
        winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --interactive
        if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 3010) {
            OK "Docker Desktop 安裝完畢！(可能需要重開機或重新開啟終端機才能使用)"
            $hasDocker = $true
        } else {
            Warn "winget 安裝 Docker Desktop 失敗。請手動安裝：https://www.docker.com/products/docker-desktop/"
            $hasDocker = $false
        }
    } else {
        Warn "找不到 winget 指令，無法自動安裝。請手動安裝：https://www.docker.com/products/docker-desktop/"
        $hasDocker = $false
    }
}

$envMap = [ordered]@{
    HUB_WORKERS    = ($active -join ',')
    HUB_LOG_DIR    = ((Join-Path $root '.hub_logs') -replace '\\', '/')
    HUB_WAIT_SLICE = '300'
}
foreach ($k in ($bins.Keys | Sort-Object)) { $envMap[$k] = $bins[$k] }

# 第 4~7 步只對「clone 下來當專案用」有意義。以 plugin 安裝時 -DepsOnly 全部跳過。
if (-not $DepsOnly) {

# --- 4. git repo -------------------------------------------------------
Step 4 "確認 git repo（worktree 派工的前提）"
Push-Location $root
try {
    $inRepo = (Invoke-Native { git rev-parse --git-dir }).Code -eq 0
    if (-not $inRepo) {
        $r = Invoke-Native { git init }
        if ($r.Code -ne 0) { Write-Host $r.Out; Die "git init 失敗" }
        OK "已 git init"
    }
    $hasHead = (Invoke-Native { git rev-parse HEAD }).Code -eq 0
    if ($hasHead) {
        OK "repo 已有 commit"
    } else {
        Invoke-Native { git add -A } | Out-Null
        $r = Invoke-Native { git commit -m "chore: bootstrap multi-agent hub" }
        if ($r.Code -eq 0) {
            OK "已建立初始 commit"
        } else {
            Warn "無法自動 commit（通常是還沒設 git user.name / user.email）。"
            Warn "git worktree 需要至少一個 commit，請手動補：git add -A; git commit -m init"
        }
    }
} finally { Pop-Location }

# --- 5. .gitignore -----------------------------------------------------
Step 5 "寫入 .gitignore"
# CLAUDE.md 必須維持未追蹤：worktree 是本 repo 的 checkout，
# 一旦進版控，worker 端的 claude 會在 worktree 讀到 Master SOP，
# 誤以為自己是編排器並再次派工（遞迴分裂）。
# 注意：.mcp.json 配合 plugin 規格需進版控，不再列入 .gitignore。
$want = @('CLAUDE.md', '.hub_logs/', '.hub_prompt.md', 'wt-*/', 'NOTES.md', '__pycache__/', '*.bak', '.claude/settings.local.json')
$gi = Join-Path $root '.gitignore'
$existing = @()
if (Test-Path -LiteralPath $gi) { $existing = @(Get-Content -LiteralPath $gi) }
$add = @($want | Where-Object { $existing -notcontains $_ })
if ($add.Count -gt 0) {
    $out = $existing + @('', '# --- multi-agent hub: machine-specific, must not reach a worktree ---') + $add
    Set-Content -LiteralPath $gi -Value $out -Encoding ascii
    OK "新增 $($add.Count) 條規則"
} else {
    OK "規則已齊全"
}

# --- 6. 派工 SOP -> CLAUDE.md（單一真實來源：skill）---------------------
Step 6 "從 multi-agent-dispatch skill 生成 CLAUDE.md（唯一菜單來源）"
# 派工 SOP 與模型菜單只維護 skills/multi-agent-dispatch/SKILL.md 一份。
# plugin 用戶直接吃這個 skill（隨 plugin 更新自動同步），repo 用戶則由這裡
# 從同一份 skill 生成 CLAUDE.md —— 兩條路徑同源，菜單不會分裂。
$skill = Join-Path $root 'skills/multi-agent-dispatch/SKILL.md'
$claudeMd = Join-Path $root 'CLAUDE.md'
if (-not (Test-Path -LiteralPath $skill)) { Die "找不到 skills/multi-agent-dispatch/SKILL.md，repo 不完整。" }
if ((Test-Path -LiteralPath $claudeMd) -and (-not $Force)) {
    Warn "CLAUDE.md 已存在，保留原檔（要覆蓋請加 -Force）"
} else {
    # 剝掉 skill 的 YAML frontmatter，換上 CLAUDE.md 專屬前言後寫出。
    # 必須 -Encoding UTF8：PS 5.1 預設用系統 ANSI(CP950) 讀，會把 UTF-8 中文打成亂碼。
    $skillRaw = Get-Content -LiteralPath $skill -Raw -Encoding UTF8
    $body = [regex]::Replace($skillRaw, '(?s)^﻿?---.*?\r?\n---\r?\n', '')
    $preamble = @'
# Multi-Agent Master Orchestrator 核心協議

> 這份檔案由 `install.ps1` 從 `skills/multi-agent-dispatch/SKILL.md` **自動生成**（已列入 .gitignore）。
> 派工 SOP 與模型菜單的唯一真實來源是那個 skill；**請勿手動編輯本檔**——改 skill 後重跑 `install.ps1 -Force`。
>
> **為什麼要生成而不是直接叫 CLAUDE.md**：worktree 是本 repo 的 checkout，
> 若 `CLAUDE.md` 進了版控，worker 端的 `claude` 會在 worktree 裡讀到這份 SOP，
> 誤以為自己是編排器而開始二次派工。維持 CLAUDE.md 未追蹤即可根治。
>
> Codex / Cursor 使用者：改生成成 `AGENTS.md` 或 `.cursorrules`（同樣別進版控）。

---

'@
    [IO.File]::WriteAllText($claudeMd, ($preamble + $body), (New-Object Text.UTF8Encoding $false))
    OK "已從 skill 生成 CLAUDE.md（單一來源）"
}

# --- 7. .mcp.json ------------------------------------------------------
Step 7 "產生 .mcp.json（Claude Code 專案級 MCP 設定）"
$hubPy = Join-Path $root 'mcp_worker_hub.py'
if (-not (Test-Path -LiteralPath $hubPy)) { Die "找不到 mcp_worker_hub.py，repo 不完整。" }

$hubPyPath = ($hubPy -replace '\\', '/')
$hub = [ordered]@{
    command = $pyExe
    args    = @($pyArgs + @($hubPyPath))
    env     = $envMap
}

$cfgPath = Join-Path $root '.mcp.json'
$isPluginConfig = $false
if (Test-Path -LiteralPath $cfgPath) {
    $rawContent = Get-Content -LiteralPath $cfgPath -Raw -ErrorAction SilentlyContinue
    if ($rawContent -and $rawContent.Contains('${CLAUDE_PLUGIN_ROOT}')) {
        $isPluginConfig = $true
    }
}

if ($isPluginConfig) {
    # ${CLAUDE_PLUGIN_ROOT} 只有「以 plugin 安裝」時才會展開。直接把 repo 當專案開時，
    # Claude Code 會把同一個檔案當專案級 MCP 讀進去、路徑不展開 → 啟動即 CONNECTION_CLOSED。
    # 所以這裡不覆寫原檔，改成：用絕對路徑註冊到 local scope，再停用專案級那份。
    Warn "根目錄 .mcp.json 是 Plugin 設定檔（含 `${CLAUDE_PLUGIN_ROOT}），保留不覆寫。"

    $claudeExe = $bins['HUB_BIN_CLAUDE_CLI']
    if (-not $claudeExe) {
        $cc = Get-Command claude -ErrorAction SilentlyContinue
        if ($cc) { $claudeExe = $cc.Source }
    }
    if (-not $claudeExe) {
        Warn "找不到 claude 執行檔，無法自動註冊 local scope，請手動跑 claude mcp add agent-hub -s local ..."
    } else {
        $addArgs = @('mcp', 'add', 'agent-hub', '-s', 'local')
        foreach ($k in $envMap.Keys) { $addArgs += @('-e', "$k=$($envMap[$k])") }
        $addArgs += @('--', $pyExe) + $pyArgs + @($hubPyPath)
        Push-Location $root
        try {
            Invoke-Native { & $claudeExe mcp remove agent-hub -s local } | Out-Null
            $r = Invoke-Native { & $claudeExe @addArgs }
            if ($r.Code -eq 0) { OK "agent-hub 已註冊到 local scope（絕對路徑）" }
            else { Write-Host $r.Out; Warn "claude mcp add 失敗，請照上面的錯誤手動註冊。" }
        } finally { Pop-Location }
    }

    $lsDir = Join-Path $root '.claude'
    if (-not (Test-Path -LiteralPath $lsDir)) { New-Item -ItemType Directory -Path $lsDir | Out-Null }
    $lsPath = Join-Path $lsDir 'settings.local.json'
    $ls = [pscustomobject]@{}
    if (Test-Path -LiteralPath $lsPath) { $ls = Get-Content -LiteralPath $lsPath -Raw | ConvertFrom-Json }
    $ls | Add-Member -NotePropertyName disabledMcpjsonServers -NotePropertyValue @('agent-hub') -Force
    [IO.File]::WriteAllText($lsPath, ($ls | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false))
    OK "已在 .claude/settings.local.json 停用專案級 agent-hub（避免重複載入而失敗）"
} else {
    $servers = [ordered]@{}
    if (Test-Path -LiteralPath $cfgPath) {
        $old = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
        if ($old.mcpServers) {
            foreach ($p in $old.mcpServers.PSObject.Properties) {
                if ($p.Name -ne 'agent-hub') { $servers[$p.Name] = $p.Value }
            }
        }
        if ($servers.Count -gt 0) { OK "保留既有的 $($servers.Count) 個 MCP server" }
    }
    $servers['agent-hub'] = $hub
    $json = ConvertTo-Json ([ordered]@{ mcpServers = $servers }) -Depth 6
    # 不能有 BOM：JSON 解析器會把它當非法字元
    [IO.File]::WriteAllText($cfgPath, $json, (New-Object Text.UTF8Encoding $false))
    OK "已寫入 $cfgPath"
}

} else {
    Step 4 "-DepsOnly：跳過 git / .gitignore / CLAUDE.md / .mcp.json（plugin 已自帶）"
}

# --- 8. 煙霧測試 -------------------------------------------------------
Step 8 "自我測試：匯入 hub、驗證工具行為與 Worker 解析"
foreach ($k in $envMap.Keys) { Set-Item -Path "env:$k" -Value $envMap[$k] }
$env:PYTHONIOENCODING = 'utf-8'   # 否則子行程用系統 ANSI 輸出，中文在主控台會亂碼

Push-Location $root
try {
    $r = Invoke-Native { & $pyExe @pyArgs test_hub.py }
    if ($r.Code -ne 0) { Write-Host $r.Out; Die "自我測試未通過，錯誤訊息如上。" }
    # 2>&1 會把 stderr 包成 ErrorRecord，Out-String 連帶印出一堆 PS 追蹤裝飾，
    # 這裡只留 hub 自己的兩種訊息。
    foreach ($line in ($r.Out -split "`r?`n")) {
        $t = ($line.Trim() -replace '^\S+\.exe\s*:\s*', '')
        if ($t -match '^(SMOKE|\[hub\]|\[\d+\])') { OK $t }
    }
} finally { Pop-Location }

# --- 完成 --------------------------------------------------------------
Write-Host "`n部署完成。" -ForegroundColor Green
Write-Host "  啟用的 Worker : $($envMap.HUB_WORKERS)"
if ($DepsOnly) {
    Write-Host "  模式          : -DepsOnly（MCP 設定與 SOP 由 plugin 提供）"
} else {
    Write-Host "  MCP 設定      : .mcp.json（Claude Code 專案級）"
    Write-Host "  Master SOP    : CLAUDE.md"
}
if (-not $hasDocker) {
    Write-Host "`n注意：本機沒有 Docker，run_in_sandbox 會回 rc=127。" -ForegroundColor Yellow
    Write-Host "      裝 Docker Desktop，或依 CLAUDE.md 的規定改在 worktree 內直接跑測試。" -ForegroundColor Yellow
}
if ($DepsOnly) {
    Write-Host "`n下一步：重開 Claude Code（plugin 的 agent-hub 會自動載入），然後："
} else {
    Write-Host "`n下一步：在本目錄開啟 Claude Code，核准 agent-hub 這個 MCP server，然後："
}
Write-Host "  /mcp                    # 確認 agent-hub 是 connected" -ForegroundColor White
Write-Host "  請呼叫 get_active_workers" -ForegroundColor White
