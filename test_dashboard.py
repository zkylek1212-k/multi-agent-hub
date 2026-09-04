"""_dash_state() 與 _log_tail() 的最小自我檢查。

沒有測試框架，`py -3 test_dashboard.py` 直接執行。
"""

import asyncio
import os
import sys
import tempfile
import time

import mcp_worker_hub as h


def _fake_job(jid, worker, desc, state, done, elapsed=None, log="fake.log", workdir="/tmp/wt"):
    ev = asyncio.Event()
    if done:
        ev.set()
    h.jobs[jid] = {
        "state": state,
        "log": log,
        "done": ev,
        "worker": worker,
        "desc": desc,
        "dir": workdir,
        "started": h.time.monotonic() - (elapsed or 0),
    }
    if elapsed is not None and done:
        h.jobs[jid]["elapsed"] = elapsed


def test_log_tail_nonexistent():
    # 1. _log_tail() 讀不存在的檔案不會炸，回傳字串（空字串）
    res = h._log_tail("non_existent_file_path_xyz_12345.log")
    assert isinstance(res, str), f"_log_tail 應回傳 str，但得到 {type(res)}"
    assert res == "", f"_log_tail 不存在檔案應回傳空字串，但得到 {res!r}"
    print("[1] _log_tail 讀不存在檔案不會炸且回傳空字串 OK")


def test_log_tail_n_lines():
    # 2. _log_tail() 只取尾端 n 行
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        lines = [
            "line 1",
            "line 2",
            "",
            "line 3",
            "   ",
            "line 4",
            "line 5",
            "line 6",
            "line 7",
            "line 8",
        ]
        f.write("\n".join(lines) + "\n")
        tmp_path = f.name

    try:
        # 非空白行共 8 行：line 1..8
        # 取尾端 3 行
        res_3 = h._log_tail(tmp_path, n=3)
        assert res_3 == "line 6\nline 7\nline 8", f"_log_tail(n=3) 結果不如預期:\n{res_3!r}"

        # 預設 n=6
        res_def = h._log_tail(tmp_path)
        assert res_def == "line 3\nline 4\nline 5\nline 6\nline 7\nline 8", f"_log_tail(n=6) 結果不如預期:\n{res_def!r}"

        # n 大於總行數時回傳全部非空行
        res_all = h._log_tail(tmp_path, n=20)
        assert res_all == "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8", f"_log_tail(n=20) 結果不如預期:\n{res_all!r}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    print("[2] _log_tail 只取尾端 n 行 OK")


def test_dash_state_structure():
    # 3. _dash_state() 在有 fake job 時回傳的結構含 jobs 清單與 hub 自身狀態欄位
    orig_jobs = h.jobs.copy()
    orig_events = list(h._HUB["events"])

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        f.write("running task log 1\nrunning task log 2\n")
        log_run_path = f.name

    try:
        h.jobs.clear()

        _fake_job(
            "job1", "agy_cli", "即時處理任務",
            "Running on agy_cli", done=False, elapsed=30,
            log=log_run_path, workdir="C:/hubdemo/wt1"
        )
        _fake_job(
            "job2", "claude_cli", "產生文件",
            "Completed\n已新增 1 個檔案", done=True, elapsed=80,
            log="nonexistent_done.log", workdir="C:/hubdemo/wt2"
        )
        _fake_job(
            "job3", "claude_cli", "執行測試",
            "Failed (rc=1)\nAssertionError: failed", done=True, elapsed=15,
            log="nonexistent_fail.log", workdir="C:/hubdemo/wt3"
        )
        _fake_job(
            "job4", "ollama", "分析日誌",
            "Crashed: ConnectionRefusedError", done=True, elapsed=5,
            log="nonexistent_crash.log", workdir="C:/hubdemo/wt4"
        )

        h._hub_event("測試事件派工")

        state = h._dash_state()

        # 頂層結構檢驗
        assert isinstance(state, dict), f"_dash_state 應回傳 dict，但得到 {type(state)}"
        for top_key in ("jobs", "hub", "ts"):
            assert top_key in state, f"_dash_state 回傳缺少頂層欄位 {top_key!r}"
        assert isinstance(state["jobs"], list), "state['jobs'] 應為 list"
        assert isinstance(state["hub"], dict), "state['hub'] 應為 dict"
        assert isinstance(state["ts"], int), "state['ts'] 應為 int 時間戳"
        assert abs(state["ts"] - int(time.time())) <= 5, "state['ts'] 時間戳差距過大"

        # hub 自身狀態欄位檢驗（欄位名以實際程式碼為準）
        hub = state["hub"]
        hub_expected_keys = {"workers", "uptime", "total", "running", "done", "failed", "events"}
        assert hub_expected_keys <= set(hub.keys()), (
            f"hub 狀態欄位缺漏: {hub_expected_keys - set(hub.keys())}"
        )
        assert hub["workers"] == h.ACTIVE, "hub['workers'] 不符"
        assert isinstance(hub["uptime"], int) and hub["uptime"] >= 0, f"hub['uptime'] 異常: {hub['uptime']}"
        assert hub["total"] == 4, f"hub['total'] 應為 4，但得到 {hub['total']}"
        assert hub["running"] == 1, f"hub['running'] 應為 1，但得到 {hub['running']}"
        assert hub["done"] == 1, f"hub['done'] 應為 1，但得到 {hub['done']}"
        assert hub["failed"] == 2, f"hub['failed'] 應為 2 (1 個 Failed + 1 個 Crashed)，但得到 {hub['failed']}"

        # events 欄位檢驗
        assert isinstance(hub["events"], list), "hub['events'] 應為 list"
        assert any(e.get("msg") == "測試事件派工" for e in hub["events"]), "未在 hub['events'] 找到剛送出的事件"
        for ev in hub["events"]:
            assert "t" in ev and "msg" in ev, f"event 項目缺少欄位: {ev}"
            assert isinstance(ev["t"], int), f"event['t'] 應為 int: {ev['t']}"
            assert isinstance(ev["msg"], str), f"event['msg'] 應為 str: {ev['msg']}"

        # jobs 列表與個別欄位檢驗
        jobs_list = state["jobs"]
        assert len(jobs_list) == 4, f"jobs 長度應為 4，但得到 {len(jobs_list)}"

        job_expected_keys = {"id", "worker", "desc", "done", "status", "elapsed", "tail", "dir"}
        for j in jobs_list:
            assert job_expected_keys <= set(j.keys()), (
                f"job 欄位缺漏: {job_expected_keys - set(j.keys())}"
            )
            assert isinstance(j["id"], str)
            assert isinstance(j["worker"], str)
            assert isinstance(j["desc"], str)
            assert isinstance(j["done"], bool)
            assert isinstance(j["status"], str)
            assert isinstance(j["elapsed"], int)
            assert isinstance(j["tail"], str)
            assert isinstance(j["dir"], str)

        job_map = {j["id"]: j for j in jobs_list}

        j1 = job_map["job1"]
        assert j1["worker"] == "agy_cli"
        assert j1["desc"] == "即時處理任務"
        assert j1["done"] is False
        assert j1["status"] == "running", f"執行中 job status 應為 'running'，實際: {j1['status']}"
        assert j1["dir"] == "C:/hubdemo/wt1"
        assert "running task log 2" in j1["tail"], f"tail 未正確讀取 log: {j1['tail']}"

        j2 = job_map["job2"]
        assert j2["worker"] == "claude_cli"
        assert j2["desc"] == "產生文件"
        assert j2["done"] is True
        assert j2["status"] == "Completed", f"完成之 job status 應為 'Completed'，實際: {j2['status']}"
        assert j2["elapsed"] == 80
        assert j2["tail"] == ""
        assert j2["dir"] == "C:/hubdemo/wt2"

        j3 = job_map["job3"]
        assert j3["done"] is True
        assert j3["status"] == "Failed (rc=1)", f"失敗之 job status 應為 'Failed (rc=1)'，實際: {j3['status']}"
        assert j3["elapsed"] == 15

        j4 = job_map["job4"]
        assert j4["done"] is True
        assert j4["status"].startswith("Crashed:"), f"崩潰之 job status 應以 Crashed: 開頭，實際: {j4['status']}"
        assert j4["elapsed"] == 5

    finally:
        try:
            os.unlink(log_run_path)
        except OSError:
            pass
        h.jobs.clear()
        h.jobs.update(orig_jobs)
        h._HUB["events"] = orig_events

    print("[3] _dash_state fake job 結構與各欄位 OK")


def main():
    test_log_tail_nonexistent()
    test_log_tail_n_lines()
    test_dash_state_structure()
    print("\nDASHBOARD TESTS OK")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nSELFTEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}", file=sys.stderr)
        sys.exit(1)
