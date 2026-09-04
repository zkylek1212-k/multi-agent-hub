# 5 分鐘快速上手 Multi-Agent Hub

## 1. 這個 Hub 在做什麼？
- **中央編排**：Master Agent 負責任務拆解、平行派工與品質把關。
- **實體隔離**：每個 CLI Worker 分配獨立的 Git Worktree，避免檔案衝突。
- **安全收斂**：Worker 完工並通過沙盒測試後，才由 Master 合併回主線。

---

## 2. 快速安裝（Windows 最短路徑）

在 PowerShell 執行一鍵安裝腳本：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

> **Plugin 模式**：若以 Claude Code plugin 方式安裝，請加上 `-DepsOnly`：
> `powershell -ExecutionPolicy Bypass -File <plugin目錄>\install.ps1 -DepsOnly`

---

## 3. 確認安裝成功

執行內建自我測試，確認 MCP Hub 與 Worker 偵測運作正常：

```powershell
py -3 test_hub.py
```
> 輸出 `test_hub.py: ALL PASSED` 即代表環境與相依套件皆已就緒。

---

## 4. 第一次派工流程（最小實例）

Master Agent 透過 MCP 工具依序執行以下 6 步：

### Step 1: 查詢可用 Worker
呼叫 `get_active_workers` 確認本機已偵測並啟用的 Worker 清單（如 `agy_cli`、`claude_cli`）。

### Step 2: 建立獨立 Worktree
呼叫 `git` 工具建立隔離工作區與分支（**必須使用絕對路徑**）：
- `args`: `"worktree add -b worker-task-1 C:/proj/wt-task-1"`
- `repo`: `"C:/proj"`

### Step 3: 派發任務
呼叫 `delegate_to_worker` 派工給指定 Worker：
- `worker_type`: `"agy_cli"`
- `working_dir`: `"C:/proj/wt-task-1"`
- `model`: `"gemini-3.7-flash-medium"`
- `prompt`: `"新增 hello.txt 寫入 OK。請將說明寫入 NOTES.md，完成後務必 git add -A 並 git commit。"`

### Step 4: 開啟即時儀表板
呼叫 `open_dashboard()` 彈出桌面懸浮視窗，即時監控各 Job 執行狀態與 Log 尾行。

### Step 5: 等待任務完成
呼叫 `wait_for_job` 批次等待（多個 job_id 以逗號分隔）：
- `job_ids`: `"<job_id>"`
- 若回傳 `[Still Running]` 則原樣再次呼叫，直到完成。

### Step 6: 驗證與合併收斂
1. 檢查是否有新 commit：`git(args="log --oneline -1", repo="C:/proj/wt-task-1")`
2. 審查改動：`git(args="diff --stat", repo="C:/proj/wt-task-1")`
3. 合併分支：`git(args="merge --no-ff worker-task-1", repo="C:/proj")`
4. 清理工作區：`git(args="worktree remove --force C:/proj/wt-task-1", repo="C:/proj")`

---

## 5. 常見卡關三則

1. **Worker 不在名單**
   - **原因**：系統未安裝該 CLI 工具或未加入 PATH，或被 `HUB_WORKERS` 排除。
   - **解法**：呼叫 `get_active_workers` 查看可用清單，勿派工給未啟用的 Worker。如需指定路徑，可在環境變數設定 `HUB_BIN_<WORKER>`（如 `HUB_BIN_CLAUDE_CLI`）。

2. **Worker 沒 commit，成果消失**
   - **原因**：Worker 結束前未執行 `git commit`，檔案僅停留在工作區。
   - **解法**：Prompt 中**必須明確要求**「完成後務必執行 `git add -A` 再 `git commit`」。Master 合併前先以 `git log -1` 檢查新 commit，若無新 commit 應要求重跑，否則後續 `worktree remove` 會將未提交檔案清空。

3. **Worktree remove 被 OneDrive 或防毒鎖住**
   - **原因**：Windows 檔案鎖定（OneDrive 同步中或背景掃描）導致 `Permission denied`。
   - **解法**：若 `worktree remove --force` 報錯，改執行 `git worktree prune` 收尾清理即可，不影響主線與已合併成果。
