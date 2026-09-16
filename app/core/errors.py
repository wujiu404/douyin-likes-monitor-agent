"""领域异常。

分两类，**不要混用**：

- `ScanSkipped` —— 本轮没东西可做（比如一个启用中的账号都没有）。
  这是**正常状态**，不是故障。图会正常走到 END，轮次台账里 error_count 保持 0，
  对外返回 409（可以解释的、由调用方修正的状态），而不是 500。
- `ProviderError`（在 `app/providers/base.py`）—— 外部数据源全链路失败。
  这是故障，要记进 error_count 并在日志里报警。

之所以要把"跳过"从"错误"里拆出来：空账号列表曾经被抛成 RuntimeError，
最后被记成「降级链全部失败」，把人误导到 provider 上去排查——
其实该去账号页加账号。
"""
from __future__ import annotations


class ScanSkipped(RuntimeError):
    """本轮扫描被跳过（没有可做的工作）。不是错误。"""


__all__ = ["ScanSkipped"]
