"""可信时间戳：只提供接口，本工具未接入任何时间戳服务。

清单的 `timestamp_token` 是接口位。生成清单时，先对“去掉 timestamp_token 与 manifest_hash 的清单内容”
计算摘要（content digest），交给时间戳服务签发令牌，再把令牌写入 `timestamp_token`，
最后计算覆盖令牌在内的 `manifest_hash`。默认实现不签发任何令牌（返回 None，清单里为 null）。

哈希链只能证明内容自清单生成以来未被改动，不能证明采集时间真实；
要证明“某时刻之前已存在”，需要另行接入可信时间戳服务，并由第三方核验令牌。
`evidence verify` 不核验时间戳令牌。
"""

from __future__ import annotations

from typing import Any, Protocol


class TimestampProvider(Protocol):
    """可信时间戳服务的接口。stamp 接收清单内容摘要（SHA-256 十六进制），返回令牌或 None。"""

    name: str

    def stamp(self, digest: str) -> dict[str, Any] | None: ...


class NullTimestampProvider:
    """空实现：不联网、不签发令牌。"""

    name = "none"

    def stamp(self, digest: str) -> dict[str, Any] | None:
        return None
