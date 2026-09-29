"""
结构化日志 —— root logger 输出 JSON 行到 stderr。

用法：
    from .logutil import setup_logging
    setup_logging()
    logging.getLogger("cc_switch_ui.server").info(
        "request", extra={"extra_fields": {"method": "GET", ...}}
    )

每行形如 {"ts": ..., "level": ..., "msg": ..., ...extra_fields}，
供 SRE 侧按行采集解析（run.sh 负责轮转）。
"""

import json
import logging
import sys
from datetime import datetime, timezone


class JsonLineFormatter(logging.Formatter):
    """把 LogRecord 序列化为单行 JSON；extra_fields 展开为顶层字段。"""

    def format(self, record):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["stack"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key not in payload:
                    payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging():
    """root logger → stderr，JSON 行格式；重复调用幂等。"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    root.handlers[:] = [handler]
    return root
