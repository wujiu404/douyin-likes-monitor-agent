"""监控主图装配。

```
START
  → load_accounts        读配置 + 取启用账号          （只读，唯一读配置表的地方）
  → collect_videos       降级链采集                  （只读外部）
  → write_snapshots      写《视频快照表》             （写，幂等键 run_id:video_id）
  → compute_deltas       算增量                      （只读上一轮快照）
  → decide_alerts        判阈值 + 写《增量与告警表》   （写，幂等键 run_id:video_id:delta）
  → [条件边] 有告警 → send_alerts   推提醒            （写，幂等键 run_id:video_id:alert）
  → run_comment_graph    fire-and-forget 起评论子图   （不阻塞，主图立刻 END）
END
```

关于降级路由：provider 降级链收在 `collect_videos` 节点内部，**不做成三条条件边**——
降级是采集的实现细节，不是业务流程的分支。图只暴露一个「采集成功 / 失败」的结果。

关于条件边：只在「有告警」这一处分流。其余全串行——没有为了并行而并行的分支。
"""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from app.graph.state import MonitorState
from app.nodes.accounts import make_load_accounts
from app.nodes.alert import make_decide_alerts, make_send_alerts
from app.nodes.collect import make_collect_videos
from app.nodes.comment_trigger import make_run_comment_graph
from app.nodes.delta import make_compute_deltas
from app.nodes.snapshot import make_write_snapshots

log = logging.getLogger(__name__)


def build_monitor_graph(deps) -> object:
    graph = StateGraph(MonitorState)

    graph.add_node("load_accounts", make_load_accounts(deps.storage, deps.registry))
    graph.add_node("collect_videos", make_collect_videos(deps.registry))
    graph.add_node("write_snapshots", make_write_snapshots(deps.storage))
    graph.add_node("compute_deltas", make_compute_deltas(deps.storage))
    graph.add_node("decide_alerts", make_decide_alerts(deps.storage))
    graph.add_node("send_alerts", make_send_alerts(deps.storage, deps.notifier))
    # 评论发射器**间接引用**（而不是把 deps.comment_runner 直接绑死）：
    # 这样测试里换掉 deps.comment_runner 就能立刻生效，不用重建整张图。
    graph.add_node(
        "run_comment_graph",
        make_run_comment_graph(deps.settings, lambda *a: deps.comment_runner(*a)),
    )

    graph.add_edge(START, "load_accounts")
    graph.add_edge("load_accounts", "collect_videos")
    graph.add_edge("collect_videos", "write_snapshots")
    graph.add_edge("write_snapshots", "compute_deltas")
    graph.add_edge("compute_deltas", "decide_alerts")

    # 唯一的分叉：没有告警就直接去起评论子图（省掉一次推送）
    graph.add_conditional_edges(
        "decide_alerts",
        lambda s: "send_alerts" if s.get("alerts") else "run_comment_graph",
        {"send_alerts": "send_alerts", "run_comment_graph": "run_comment_graph"},
    )
    graph.add_edge("send_alerts", "run_comment_graph")
    graph.add_edge("run_comment_graph", END)

    return graph.compile(checkpointer=deps.checkpointer)
