# 景区客流联动服务

面向大型景区值班室的可独立运行服务：把闸机计数、线上分时票、停车区入场、摆渡车与
图像计数等**可能迟到、乱序、重复**的事件归并为各区域**可信占用量**，结合步行通道、
摆渡运力与临时封闭生成分流动作；支持有期限、有理由、有责任人的人工接管；
重启后从追加日志恢复正在执行的措施，并可回放任意一次拥堵前后的完整决策链。

> 夹具中的容量与时刻均为本地开发示例，不代表任何真实景区。

## 运行

```bash
python -m unittest discover -s tests     # 38 项测试
python -m src.api --port 8080            # 启动值班接口（仅标准库）
```

## 数据流与模块

```
事件 ──▶ occupancy（归并/台账/快照/新鲜度/分时票/跨日）
            │  OccupancyReport（estimated 实测 + predicted 仅辅助）
            ▼
        policy（风险分级 → 动作；消防硬限、特殊通道、返程保障、斜坡）
            ▲ overrides（人工接管：期限/理由/责任人）  topology（通道/封闭/运力）
            │
        service（门面：序号、tick、回执、持久化、重启恢复）
            │ store：只追加 JSONL（event/override/closure/decision/receipt）
            ▼
        api（/status /events /ticks /overrides /closures /receipts /chain）
        replay（从日志按时间轴重建，并与历史决策逐字段比对）
```

| 文件 | 职责 |
| --- | --- |
| `src/events.py` | 事件模型、类型、幂等键、迟到/未来事件边界 |
| `src/occupancy.py` | 去重、增量台账、图像快照融合、新鲜度缓冲、分时票队列、跨日重置 |
| `src/topology.py` | 步行/摆渡边、核定运力、带期限的临时封闭 |
| `src/overrides.py` | 人工接管登记与到期失效 |
| `src/policy.py` | 风险分级、预限流/分流/摆渡/返程、硬限裁剪、斜坡放行状态机 |
| `src/store.py` | 追加式 JSONL 日志（flush+fsync） |
| `src/service.py` | 事务门面、执行回执、恢复 |
| `src/replay.py` | 决策链回放与确定性校验 |
| `src/api.py` | stdlib HTTP 接口 |

## 关键安全语义

1. **可信占用量**：`max(去负后台账, 最新新鲜图像快照)`；来源上报后中断（`stale`）
   时按容量与失联比例附加保守缓冲，从未上报（`silent`）不虚构在园人数。
2. **消防容量是硬限**：仅由**实测可信占用**触发“只出不进”，并联动拦截所有上游通道；
   预测占用（分时票队列）只能产生 `advisory` 建议，永不单独触限。
3. **人工接管不能突破底线**：接管期间自动动作挂起并在决策中留痕；
   消防硬限、特殊人群通道（`special_lanes`）、闭园返程保障仍强制叠加，
   冲突的人工放行动作被改写为只出不进。每次接管必须有期限（单次≤12h）、理由、责任人，
   到期自动失效。
4. **恢复自动不突然释放积压**：接管结束后按 `ramp_release_ratio_per_minute`
   （默认每分钟 5% 容量）斜坡放行；若此时仍处硬限或返程保障窗口，斜坡挂起，
   阻塞解除那一刻才开始计时；放坡途中再次触限则撤销重来。
5. **返程保障**：闭园前 90 分钟起入口只出不进、摆渡优先向出口疏运，
   闭园后 60 分钟内仍保障在园游客离园。
6. **跨日确定重置**：新运营日首次到达开园时刻后，台账/快照/来源状态清零，
   未核销的旧时票作废、未来日预约保留。

## 值班接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /status` | 当前风险、占用（台账/快照/缓冲/预测）、逐来源新鲜度、接管与封闭、动作回执 |
| `POST /events` | 上报事件（重复返回 202 + `duplicate`，过迟/未来/未知区域均被拒绝并留痕） |
| `POST /ticks` | 驱动一次决策，返回动作、依据、被抑制建议、斜坡上限 |
| `POST /overrides` / `DELETE /overrides?id=` | 人工接管登记/解除（缺期限/理由/责任人返回 400） |
| `POST /closures` / `POST /closures/reopen` | 临时封闭（同样强制理由与责任人） |
| `POST /receipts` | 动作执行回执（acknowledged/executed/failed/skipped） |
| `GET /chain?decision_id=&window=5` | 某次决策前后完整决策链（决策、读数证据、措施变动、回执） |

确定性回放（校验历史决策可重算）：

```python
from src.replay import replay
result = replay(zones, links, "data/journal.jsonl")
assert result.mismatches == []
```

测试覆盖闸机离线、重复/乱序/迟到事件、图像快照分歧、跨日闭园、消防硬限穿透接管、
斜坡等待与重启续放、措施/回执恢复及回放一致性。
