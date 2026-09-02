# 安裝與部署指南

從全新機台 clone 到能派工，Windows 上兩行指令。
架構原理、安全模型與實測紀錄見 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## TL;DR

```powershell
git clone <你的 repo 網址> multi-agent-hub; cd multi-agent-hub
```

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

然後在同一個目錄開 Claude Code，核准 `agent-hub` 這個 MCP server 就能用了。

---

## 1. 需要先裝什麼

`install.ps1` 對外部工具的處理分兩種，請先看清楚：

- **Docker Desktop —— 會自動裝**。偵測不到 `docker` 時，腳本會呼叫
  `winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --interactive`
  嘗試自動安裝。過程會**跳出 UAC 提權視窗**與 Docker 自己的安裝畫面，裝完**可能需要重開機**
  （至少要重開終端機）`docker` 才會出現在 PATH。不想被自動安裝，就先自己裝好 Docker；
  機器上沒有 `winget` 時腳本只會警告並繼續。
- **其他工具（Python、git、Worker CLI）—— 只偵測、不安裝**（它們各有自己的登入與授權流程）。
  找不到 Python 3.10+ 或 git 會直接中止；一個 Worker CLI 都沒偵測到也會中止。

### 必裝

| 工具 | 用途 | 取得方式 |
| --- | --- | --- |
| **Python 3.10+** | 跑 MCP Hub Server（用到 `str \| None` 語法，3.10 起才支援） | <https://www.python.org/downloads/>，安裝時勾 **Add python.exe to PATH** |
| **Git** | worktree 檔案隔離，整套架構的核心 | <https://git-scm.com/download/win> |
| **至少一個 Worker CLI** | 實際做事的人（見下表） | 見下表 |

### Worker CLI（至少裝一個，裝愈多可平行度愈高）

| Worker 名稱 | 指令 | 安裝 | 備註 |
| --- | --- | --- | --- |
| `claude_cli` | `claude` | `npm i -g @anthropic-ai/claude-code` | 需要 Claude 訂閱或 API key。**建議用官方原生安裝版**，npm 版只有 `.cmd` shim（見 §常見問題） |
| `agy_cli` | `agy` | Antigravity CLI 官方安裝程式 | 執行檔叫 `agy`，不是 `antigravity` |
| `codex_cli` | `codex` | `npm i -g @openai/codex` | 需要 OpenAI 帳號 |
| `local_70b` | `ollama` | <https://ollama.com/download> | 裝完要先 `ollama pull qwen2.5-coder:70b`。純 LLM 無讀檔能力，hub 會改用直接餵 prompt 的路徑 |

### 選裝（強烈建議）

| 工具 | 用途 | 取得方式 |
| --- | --- | --- |
| **Docker Desktop** | `run_in_sandbox` 的隔離測試環境 | 沒裝的話 `install.ps1` 會用 winget 自動裝（跳 UAC，可能要重開機）；手動裝：<https://www.docker.com/products/docker-desktop/> |

**沒有 Docker 會怎樣**：`run_in_sandbox` 回 `rc=127 找不到執行檔`。Master 的 SOP 第三階段有備援規則——改在 worktree 內直接跑測試（無隔離），並在回報中標明。**但絕不允許因此跳過測試**。

---

## 2. Windows 一鍵部署

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

可重複執行，不會弄壞既有設定。參數：

| 參數 | 用途 |
| --- | --- |
| `-SkipDeps` | 跳過 `pip install`（已裝過時加速） |
| `-Force` | 覆蓋既有的 `CLAUDE.md` |

**它做了 8 件事**：

1. 找 Python 3.10+（依序試 `py -3` → `python` → `python3`）
2. `pip install mcp[cli]`
3. 偵測 4 種 Worker + `git` + `docker`，解析出**真正的 `.exe` 路徑**
4. 沒有 git repo 就 `git init` 並建立初始 commit（worktree 需要至少一個 commit）
5. 寫 `.gitignore`
6. 把 `MASTER_SOP.md` 複製成 `CLAUDE.md`
7. 產生機器專屬的 `.mcp.json`。**若根目錄的 `.mcp.json` 內含 `${CLAUDE_PLUGIN_ROOT}`（＝plugin 版設定），
   則保留原檔不覆寫**，改印出一行 `claude mcp add agent-hub -e ... -- ...` 讓你自行決定要不要另外加成專案級設定
8. **自我測試**：跑 `test_hub.py` —— 匯入 hub、驗證 `list_jobs` 的狀態表、拒絕未啟用的 Worker、未知 job id 的處理

第 8 步過了才算部署成功。

---

## 3. macOS / Linux

沒有 `.cmd` shim 問題，所以不需要 `HUB_BIN_*`，手動四步即可：

```bash
python3 -m pip install "mcp[cli]"
```

```bash
git init && git add -A && git commit -m init   # 已是 repo 就跳過
```

```bash
cp MASTER_SOP.md CLAUDE.md
printf '.mcp.json\nCLAUDE.md\n.hub_logs/\n.hub_prompt.md\nwt-*/\nNOTES.md\n' >> .gitignore
```

`.mcp.json`（把 `<REPO>` 換成絕對路徑）：

```json
{
  "mcpServers": {
    "agent-hub": {
      "command": "python3",
      "args": ["<REPO>/mcp_worker_hub.py"],
      "env": {
        "HUB_WORKERS": "claude_cli,codex_cli",
        "HUB_LOG_DIR": "<REPO>/.hub_logs",
        "HUB_WAIT_SLICE": "300"
      }
    }
  }
}
```

驗證：

```bash
HUB_WORKERS=claude_cli python3 -c "import mcp_worker_hub as h; print(h.ACTIVE)"
```

---

## 4. 驗證部署

```powershell
claude mcp list
```

看到 `agent-hub: ... - ⏸ Pending approval` 就是設定正確。開 Claude Code 核准後會轉成 `✔ Connected`。

進 Claude Code 後：

```
/mcp
```

再對 Master 說「請呼叫 get_active_workers」，它應該列出啟用的 Worker 與各自的執行檔絕對路徑。

**冒煙派工**（確認端到端可用）：

> 幫我用 agent-hub 派一個最小任務：建立 worktree，讓 claude_cli 在裡面新增 hello.txt 內容為 OK，然後 git diff --stat 給我看。

---

## 5. 檔案說明

| 檔案 | 進版控 | 說明 |
| --- | --- | --- |
| `mcp_worker_hub.py` | ✅ | MCP Server 本體，7 個工具 |
| `MASTER_SOP.md` | ✅ | Master 系統指示詞範本 |
| `test_hub.py` | ✅ | 自我測試，`py -3 test_hub.py` 可單獨跑 |
| `install.ps1` | ✅ | 部署腳本（**必須 UTF-8 with BOM**，見常見問題） |
| `ARCHITECTURE.md` | ✅ | 架構、安全模型、實測紀錄 |
| `README.md` | ✅ | GitHub 首頁 |
| `LICENSE` | ✅ | MIT |
| `.mcp.json` | ✅ | plugin 規格要求，用 `${CLAUDE_PLUGIN_ROOT}` 而非絕對路徑，所以可以進版控 |
| `.claude-plugin/plugin.json` | ✅ | plugin manifest（名稱、版本、說明） |
| `skills/multi-agent-dispatch/SKILL.md` | ✅ | 派工 SOP 的 Skill 版，隨 plugin 安裝 |
| `CLAUDE.md` | ❌ | 由 SOP 複製而來，**故意不進版控** |
| `.hub_logs/` | ❌ | 每個 job 的完整 log 與 prompt 備份 |
| `*.bak` | ❌ | 編輯器備份，不進版控 |

**為什麼 `CLAUDE.md` 不能進版控**：worktree 是本 repo 的 checkout。`CLAUDE.md` 一旦被追蹤，worker 端的 `claude` 會在 worktree 裡讀到 Master SOP，誤以為自己是編排器並開始二次派工，遞迴分裂。維持未追蹤即可根治。

**那 `.mcp.json` 呢**：它現在因為 plugin 規格必須進版控，所以「worker 從 `.mcp.json` 撿到 hub 的派工工具」這條路改由另一道防線擋——`mcp_worker_hub.py` 的 `WORKER_CMDS` 對 `claude_cli` 固定帶 `--strict-mcp-config`，worker 就不會載入 worktree 裡的專案 `.mcp.json`，自然也拿不到 hub 的工具。（此旗標只對 `claude_cli` 有效；其餘 worker 不吃 Claude Code 的 `.mcp.json` 格式。）

---

## 6. 常見問題

**Q：`install.ps1` 一執行就噴一堆 `Unexpected token` 語法錯誤**
檔案編碼掉了。PowerShell 5.1 沒有 BOM 就用系統 ANSI（繁中是 cp950）讀 `.ps1`，中文註解會亂碼並吃掉字串的引號配對。修復：

```powershell
$p='.\install.ps1'; $t=[IO.File]::ReadAllText($p,(New-Object Text.UTF8Encoding $false)); [IO.File]::WriteAllText($p,$t,(New-Object Text.UTF8Encoding $true))
```

Git 設定裡不要對 `.ps1` 做編碼轉換。

**Q：`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`**
你裝到 mcp SDK 2.x，`FastMCP` 已改名 `MCPServer`。本 repo 的 `mcp_worker_hub.py` 已經雙相容（try/except 匯入），會自動吃 1.x 或 2.x。若你是從架構文件複製舊程式碼，請改用 repo 裡的版本，或 `pip install "mcp<2"`。

**Q：偵測到 Worker 但顯示「是批次檔」的警告**
npm 全域安裝只給 `.cmd` shim，走 cmd.exe 會竄改參數並有 8191 字命令列上限（架構文件 §6.2 / §6.3 有實測）。本架構的 prompt 走檔案傳遞，所以**不會**被注入，但仍建議指向真 `.exe`。Claude Code 的位置：

- 原生安裝版：`%USERPROFILE%\.local\bin\claude.exe`
- npm 版：`%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe`

在 `.mcp.json` 的 `env` 加 `"HUB_BIN_CLAUDE_CLI": "<上面的路徑，用正斜線>"`，重開 Claude Code。

**Q：`claude mcp list` 看不到 agent-hub**
`.mcp.json` 是**專案級**設定，必須在 repo 根目錄啟動 Claude Code 才會讀到。也確認 `.mcp.json` 沒有 BOM。

**Q：用 plugin 裝好了，但 `/mcp` 看不到 agent-hub**
依序查三件事：

1. `/plugin` 確認 `multi-agent-hub` 是 enabled（裝完通常要重開 Claude Code）。
2. plugin 版 `.mcp.json` 是用 `py -3` 啟動的。在終端機直接跑 `py -3 <plugin 目錄>/mcp_worker_hub.py`：
   噴 `py` 不存在，代表機器上沒有 Windows Python launcher；噴 `ModuleNotFoundError: No module named 'mcp'`，
   代表還沒跑過 `install.ps1`（plugin 不會幫你裝 Python 相依）。
3. 連上了但 `get_active_workers` 是空的：plugin 版 `.mcp.json` 的 `HUB_WORKERS` 留空＝改由 hub 自己掃 PATH，
   掃不到（或只掃到 npm 的 `.cmd` shim）時，就照 `install.ps1` 印出的 `claude mcp add ...` 那行補上
   `HUB_WORKERS` 與 `HUB_BIN_*`。

**Q：`git worktree add` 失敗說 repo 沒有 commit**
`git add -A; git commit -m init`。通常是 git 還沒設身分：`git config --global user.name "..."`、`git config --global user.email "..."`。

**Q：`wait_for_job` 一直回 `[Still Running]`**
正常。`HUB_WAIT_SLICE` 預設 300 秒是刻意切片，Master 應該原樣再呼叫一次。若你的 client 有 60 秒 MCP 工具上限（Claude Desktop 聊天 app、Cursor），把它降到 `45`。

**Q：Server 重啟後 job 全不見**
job 狀態存在記憶體。完整 log 還在 `.hub_logs/<job_id>.log`，prompt 備份在 `.hub_logs/<job_id>.prompt.md`。

---

## 7. 部署前務必知道的安全界線

- **Worker 寫程式階段沒有權限隔離**。Worker 帶著 `--dangerously-skip-permissions` / `--sandbox workspace-write` 直接跑在你的機器上。worktree 隔離的是「檔案版本」，不是「能做什麼」。
- **Docker 保護的是測試，不是寫程式**。
- **Worker 的輸出會進入 Master 的 context**，被污染的內容可能影響 Master 判斷。SOP 有禁令但那是軟性防線。

**只在你信任的專案上使用這套流程。**
