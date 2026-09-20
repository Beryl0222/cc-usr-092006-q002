"""独立演示：用脚本化时间线回放一次完整拥堵处置（确定性，不依赖真实时钟）。

运行：
    python -m src.demo
会在 data/demo.jsonl 写入全过程，可随后用 /replay?day=2026-09-20 查看决策链。
加 --clean 可清空旧演示日志重跑。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .domain import load_site
from .engine import ACK, EXECUTED, FlowEngine
from .events import LOCAL_TZ
from .store import Journal

# 以本地 2026-09-20 的时间线表达，转 UTC 入引擎
def _at(hh: int, mm: int, day: str = "2026-09-20") -> datetime:
    local = datetime.fromisoformat(day).replace(hour=hh, minute=mm, tzinfo=LOCAL_TZ)
    return local.astimezone(timezone.utc)


def _flow(eid, source, zone, value, at):
    return {"event_id": eid, "source": source, "type": "flow", "zone": zone,
            "value": value, "occurred_at": at.isoformat()}


def _snap(eid, zone, value, at):
    return {"event_id": eid, "source": "camera", "type": "snapshot", "zone": zone,
            "value": value, "occurred_at": at.isoformat()}


def _lead(eid, source, zone, value, at):
    return {"event_id": eid, "source": source, "type": "lead", "zone": zone,
            "value": value, "occurred_at": at.isoformat()}


def run(journal_path: str, clean: bool = False) -> FlowEngine:
    path = Path(journal_path)
    if clean and path.exists():
        path.unlink()

    site = load_site("fixtures/zones.json", "fixtures/site.json")
    eng = FlowEngine(site, Journal(path))

    def show(label, at):
        ev = eng.evaluate(at)
        p = eng.status_payload(ev)
        print(f"\n[{at.astimezone(LOCAL_TZ):%H:%M}] {label}  总体风险={p['overall_risk']} "
              f"阶段={p['phase']}")
        for zc, z in p["zones"].items():
            if z["occ"] is not None or z["risk"] != "green":
                print(f"    {z['name']:8s} 占用={z['occ']}/{z['capacity']} "
                      f"风险={z['risk']} 前瞻={z['inbound_pressure']} 置信={z['confidence']}")
        for m in p["measures"]:
            tag = "安全" if m["safety"] else "    "
            print(f"    [{m['status']:8s}] {tag} {m['code']:24s} {json.dumps(m['params'], ensure_ascii=False)}")
            print(f"              依据: {m['reason']}")
        for n in p["notes"]:
            print(f"    （恢复）{n}")
        return ev

    # 07:30 开园，一切平稳
    show("开园，游客陆续进入", _at(7, 30))

    # 08:00 停车与分时票显示前瞻压力
    eng.ingest_many([
        _flow("g0", "gate", "waterfall", 900, _at(7, 55)),
        _snap("c0", "waterfall", 900, _at(7, 58)),
        _lead("pk0", "parking", "gate-east", 600, _at(7, 59)),
        _lead("tk0", "ticket", "gate-east", 400, _at(7, 59)),
    ], _at(8, 0))
    show("停车/分时票预警未来 15-30 分钟到达", _at(8, 0))

    # 08:20 游客涌入，核心区接近红色
    eng.ingest_many([
        _snap("c1", "waterfall", 1560, _at(8, 18)),
        _flow("g1", "gate", "waterfall", 660, _at(8, 18)),
        _flow("ge1", "gate", "gate-east", 700, _at(8, 18)),
        # 同一条事件重复投递、且迟到
        _snap("c1", "waterfall", 1560, _at(8, 18)),
    ], _at(8, 20))
    show("核心区橙色：上游入口同步控流", _at(8, 20))

    # 08:35 核心区红色，同时 p-ge-wf 通道临时封闭
    eng.add_override("storm-block", "block_passage", 3600, "周调度",
                     "前方落石风险，临时封闭入口至核心区步道",
                     {"passage": "p-ge-wf", "blocked": True}, now=_at(8, 35))
    eng.ingest_many([
        _snap("c2", "waterfall", 1765, _at(8, 33)),
        _flow("g2", "gate", "waterfall", 205, _at(8, 33)),
    ], _at(8, 35))
    ev = show("核心区红色 + 主通道封闭：硬截留并给出绕行", _at(8, 35))

    # 现场回执
    eng.post_receipt("holdroute:gate-east:waterfall", ACK, "北门岗", "入口外已截留", now=_at(8, 36))
    eng.post_receipt("boost:north", ACK, "摆渡调度", "北线已增能", now=_at(8, 36))
    eng.post_receipt("boost:north", EXECUTED, "摆渡调度", "班次已上线", now=_at(8, 40))

    # 08:45 雷电，人工接管入口额度（更严）
    eng.add_override("storm-throttle", "throttle", 3600, "安全负责人-赵",
                     "雷电黄色预警，人工压减入园节奏",
                     {"zone": "gate-east", "admit_per_interval": 30}, now=_at(8, 45))
    show("雷电人工接管：入口按 30 人/窗口（安全动作仍强制）", _at(8, 45))

    # 09:20 雷电接管仍在但天气好转，占用缓解；斜坡恢复（接管额度仍生效）
    eng.ingest_many([
        _snap("c3", "waterfall", 600, _at(9, 18)),
        _flow("g3", "gate", "waterfall", -1165, _at(9, 18)),  # 净出园
    ], _at(9, 20))
    show("核心区缓解，雷电接管仍在（额度不突放）", _at(9, 20))

    # 09:25 雷电解除，撤销接管
    eng.revoke_override("storm-throttle", "安全负责人-赵", "雷电预警解除，恢复自动", now=_at(9, 25))
    show("撤销接管：自动策略按斜坡渐进恢复", _at(9, 25))
    for off in (5, 10, 15, 20):
        show(f"恢复 +{off} 分钟", _at(9, 25 + off))

    # 20:10 闭园返程保障：停止入园、摆渡满负荷、开放返程特殊通道
    eng.ingest_many([
        _snap("c4", "waterfall", 500, _at(20, 8)),
        _flow("g4", "gate", "waterfall", -100, _at(20, 8)),
    ], _at(20, 10))
    show("闭园返程保障：停入园、摆渡满负荷、开放返程通道", _at(20, 10))

    print("\n决策链已写入", journal_path, "；共", eng._tick_seq, "个评估时刻。")
    eng.journal.close()
    return eng


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="data/demo.jsonl")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args(argv)
    run(args.journal, args.clean)


if __name__ == "__main__":
    main()
