"""统一错误类型：任何阶段失败都要带上原因和可恢复操作，不允许用空结果或样本值兜底。"""

from __future__ import annotations


class PipelineError(Exception):
    """管线阶段错误。

    Args:
        status: "failed"（本步失败）或 "blocked"（前序未完成，本步不执行）
        reason: 面向用户的中文原因
        remediation: 可执行的下一步建议列表
        detail: 结构化补充信息（不写患者标识信息的值）
    """

    def __init__(
        self,
        status: str,
        reason: str,
        remediation: list[str] | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.remediation = list(remediation or [])
        self.detail = dict(detail or {})

    def to_dict(self) -> dict:
        return {
            "error": True,
            "status": self.status,
            "reason": self.reason,
            "remediation": self.remediation,
            "detail": self.detail,
        }

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        extra = "；".join(self.remediation)
        return f"[{self.status}] {self.reason}" + (f"（建议：{extra}）" if extra else "")
