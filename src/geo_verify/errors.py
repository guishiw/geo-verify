"""统一的错误类型与退出码。

退出码约定：0 成功，1 检查未通过，2 输入错误，3 数据不足，4 内部错误。
"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_INPUT_ERROR = 2
EXIT_INSUFFICIENT = 3
EXIT_INTERNAL = 4


class GeoError(Exception):
    """所有可预期错误的基类。"""

    exit_code: int = EXIT_INTERNAL
    kind: str = "internal_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.kind, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class CheckFailed(GeoError):
    """检查未通过（退出码 1）。"""

    exit_code = EXIT_CHECK_FAILED
    kind = "check_failed"


class InputError(GeoError):
    """输入错误（退出码 2）：文件缺失、格式不合契约、参数非法。"""

    exit_code = EXIT_INPUT_ERROR
    kind = "input_error"


class InsufficientData(GeoError):
    """数据不足（退出码 3）：样本或窗口不够，不能给出肯定结论。"""

    exit_code = EXIT_INSUFFICIENT
    kind = "insufficient_data"
