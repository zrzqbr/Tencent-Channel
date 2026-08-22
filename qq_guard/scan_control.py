import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScanLock:
    """Cross-process single-flight lock shared by web and scheduled scans."""

    def __init__(self, database_path: Path) -> None:
        database_path = Path(database_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = database_path.parent / "tencent-scan.lock"
        self._handle = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} thread={threading.get_ident()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class ScanStatusStore:
    """Small shared status document used by the browser progress indicator."""

    def __init__(self, database_path: Path, task_type: str = "scan") -> None:
        database_path = Path(database_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.task_type = "sync" if task_type == "sync" else "scan"
        self.path = database_path.parent / f"tencent-{self.task_type}-status.json"
        self._lock = threading.Lock()

    def start(self, job_id: str) -> Dict[str, Any]:
        state = {
            "job_id": job_id,
            "status": "running",
            "percent": 3,
            "task_type": self.task_type,
            "phase": "准备同步" if self.task_type == "sync" else "准备巡检",
            "message": (
                "正在连接腾讯频道"
                if self.task_type == "sync"
                else "正在读取后台已同步的内容"
            ),
            "started_at": _utc_now(),
            "finished_at": "",
            "summary": {},
            "error": "",
        }
        self._write(state)
        return state

    def update(
        self,
        job_id: str,
        *,
        percent: int,
        phase: str,
        message: str,
    ) -> Dict[str, Any]:
        state = self.read(job_id) or self.start(job_id)
        state.update(
            {
                "status": "running",
                "percent": max(3, min(int(percent), 98)),
                "phase": str(phase)[:80],
                "message": str(message)[:300],
            }
        )
        self._write(state)
        return state

    def complete(self, job_id: str, summary: Dict[str, Any]) -> Dict[str, Any]:
        state = self.read(job_id) or self.start(job_id)
        if self.task_type == "sync":
            phase = "同步完成"
            message = (
                f"已同步 {int(summary.get('synced_feeds') or 0)} 条内容，"
                f"新增 {int(summary.get('new_feeds') or 0)} 条，"
                f"补齐历史正文 {int(summary.get('backfilled_feeds') or 0)} 条，"
                "本次未执行 AI 分析"
            )
        else:
            phase = "巡检完成"
            message = (
                f"已分析 {int(summary.get('scanned_feeds') or 0)} 条内容，"
                f"完成文字 AI {int(summary.get('ai_reviewed') or 0)} 条，"
                f"图片检查 {int(summary.get('ai_vision_reviewed') or 0)} 条"
            )
        state.update(
            {
                "status": "completed",
                "percent": 100,
                "phase": phase,
                "message": message,
                "finished_at": _utc_now(),
                "summary": summary,
                "error": "",
            }
        )
        self._write(state)
        return state

    def fail(self, job_id: str, error: str) -> Dict[str, Any]:
        state = self.read(job_id) or self.start(job_id)
        state.update(
            {
                "status": "failed",
                "phase": "同步失败" if self.task_type == "sync" else "巡检失败",
                "message": str(error)[:300],
                "finished_at": _utc_now(),
                "error": str(error)[:500],
            }
        )
        self._write(state)
        return state

    def read(self, job_id: str = "") -> Optional[Dict[str, Any]]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if job_id and state.get("job_id") != job_id:
            return None
        return state

    def _write(self, state: Dict[str, Any]) -> None:
        with self._lock:
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(self.path)
