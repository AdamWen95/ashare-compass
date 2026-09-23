"""An OS-held process lock: a stale PID never authorizes deleting a live lock."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class AlreadyRunning(RuntimeError):
    def __init__(self, metadata: dict | None = None):
        self.metadata = metadata or {}
        super().__init__("已有日任务持有操作系统锁，未启动重复任务")


class ProcessLock:
    def __init__(self, path: Path, run_id: str):
        self.path = Path(path).absolute()
        self.run_id = str(run_id)
        if not self.run_id or len(self.run_id) > 160:
            raise ValueError("run_id 无效")
        self._file = None
        self.metadata: dict = {}
        self.previous_metadata: dict = {}
        self.recovered_stale = False

    @staticmethod
    def _read(stream) -> dict:
        try:
            stream.seek(1)
            value = json.loads(stream.read(8192).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (ValueError, UnicodeError, OSError):
            return {}

    def _write(self):
        self._file.seek(1)
        self._file.write(json.dumps(self.metadata, ensure_ascii=False).encode("utf-8"))
        self._file.truncate()
        self._file.flush()
        os.fsync(self._file.fileno())

    def acquire(self):
        if self._file is not None:
            raise RuntimeError("锁对象不可重复获取")
        if any(item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction())
               for item in (self.path, *self.path.parents)):
            raise ValueError("锁路径不能经过符号链接或 junction")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+b", buffering=0)
        try:
            # Byte zero is reserved for the OS lock; metadata starts at byte one.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            metadata = self._read(stream)
            stream.close()
            raise AlreadyRunning(metadata) from None
        self._file = stream
        try:
            self.previous_metadata = self._read(stream)
            self.recovered_stale = self.previous_metadata.get("state") == "running"
            self.metadata = {"schema_version": "m4-lock-v1", "pid": os.getpid(), "run_id": self.run_id,
                             "started_at": datetime.now(SHANGHAI).isoformat(), "state": "running",
                             "recovered_stale": self.recovered_stale,
                             "previous_run_id": self.previous_metadata.get("run_id")}
            self._write()
        except BaseException:
            self.release()
            raise
        return self

    def release(self):
        if self._file is None:
            return
        stream = self._file
        try:
            self.metadata.update(state="released", ended_at=datetime.now(SHANGHAI).isoformat())
            self._write()
        finally:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
                self._file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.release()

