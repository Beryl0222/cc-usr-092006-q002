"""追加式 JSONL 持久化。

只追加、不改写：每一行是一条带序号的记录（事件、接管、封闭、决策、回执、快照）。
服务重启时顺序回放即可恢复正在执行的措施；崩溃最多丢失最后一条未刷盘记录。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator


class JournalStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = {"kind": kind, "payload": payload}
        self._fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return record

    def read_all(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __del__(self) -> None:  # 资源兜底：忘记 close 时不在析构时告警
        try:
            self.close()
        except Exception:
            pass
