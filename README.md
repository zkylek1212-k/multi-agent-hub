# Multi-Agent Hub

用 **MCP (Model Context Protocol)** 打造的可抽換多智能體派工中心。

Master Agent 負責**拆解任務、平行派工、驗證結果**；實際寫程式交給多個 CLI Worker，
每個 Worker 在**自己的 git worktree** 裡工作，測試過了才准合併回主線。

主控端可以是 Claude Code、Claude Desktop 或 Codex / Cursor；
Worker 支援 Claude Code、Antigravity (`agy`)、Codex、Ollama —— 機器上裝了哪個就開哪個，
啟動時自動偵測，沒裝的自動停用。

## 為什麼要 worktree

平行派工最大的坑是多個 agent 同時改同一份檔案。這裡每個子任務拿一個**獨立 worktree ＋ 獨立分支**，
實體隔離，最後用 `git merge --no-ff` 收斂。發生衝突就 `merge --abort` 並回報是哪兩個子任務撞了，
不讓 agent 自己亂解。

## 架構

```
        Master Agent (Claude Code / Desktop / Cursor)
                        │ MCP over stdio
                        ▼
              mcp_worker_hub.py  ←── HUB_WORKERS / HUB_BIN_* / HUB_WAIT_SLICE
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
  Git Worktree     CLI Workers      Docker Sandbox
  （檔案隔離）      claude_cli        （跑測試，
  + .hub_prompt     agy_cli           預設無網路）
                    codex_cli
                    local_70b
```

## 快速開始

Windows，clone 完兩行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

腳本會偵測工具、裝相依、產生 `.mcp.json`，最後跑一次自我測試。
需要先裝什麼、macOS / Linux 做法、常見問題，都在 **[INSTALL.md](INSTALL.md)**。

## 工具

| 工具 | 用途 |
| --- | --- |
| `get_active_workers` | 回報本次啟用的 Worker 與實際解析到的執行檔 |
| `git` | worktree add / remove、diff、log、merge |
| `delegate_to_worker` | 非同步派工，立即回傳 job_id |
| `wait_for_job` | 一次等一整批 job（等待長度可設定，避開 client 的逾時上限） |
| `check_job_status` | 非阻塞查詢單一 job |
| `list_jobs` | 所有 job 的狀態表：job_id／Worker／狀態／耗時／任務 |
| `run_in_sandbox` | 在容器中對 worktree 跑測試（網路預設關閉） |

## ⚠️ 安全模型

隔離是**分層而且不完整**的，用之前請認知：

| 階段 | 隔離程度 |
| --- | --- |
| Worker 寫程式 | **無權限隔離**。Worker 帶著 `--dangerously-skip-permissions` 直接跑在你的機器上。worktree 隔離的是「檔案版本」，不是「能做什麼」 |
| 跑測試 | 有隔離。Docker 容器 ＋ 預設無網路 ＋ 記憶體／CPU 上限 |
| Master 讀 Worker 輸出 | **無隔離**。Worker 的 stdout 會進入 Master 的 context |

**Docker 沙盒保護的是「測試」，不是「寫程式」。只在你信任的專案上使用。**

## 文件

| 檔案 | 內容 |
| --- | --- |
| [INSTALL.md](INSTALL.md) | 安裝與部署、工具清單、常見問題 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架構原理、設計決策、實測驗證紀錄 |
| [MASTER_SOP.md](MASTER_SOP.md) | Master 的系統指示詞（`install.ps1` 會複製成 `CLAUDE.md`） |

## License

[MIT](LICENSE)
