"""追加式 JSONL 持久化。

所有输入与决策都只追加、不修改：
- event        归一化后的计数事件（按 event_id 去重在引擎层完成）
- quarantined  无法归一化的原始消息（隔离，不影响占用量）
- override     人工接管的创建/撤销
- tick         每个评估时刻的完整决策快照（动作、依据、风险、限流状态）
- receipt      执行回执（ack / executed / failed）

重启时顺序读取即可重建：事件集合、接管视图、最近一次限流状态、执行中的措施。
写入采用行缓冲 + fsync，保证崩溃后最多丢一行，且不会读到半截 JSON。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 行缓冲文本句柄；追加模式天然支持多进程串行追加的最后写入
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)

    def append(self, record: dict) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def read(self) -> Iterator[dict]:
        # 从磁盘重新读取，保证与句柄缓冲无关；跳过崩溃可能残留的空行
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
