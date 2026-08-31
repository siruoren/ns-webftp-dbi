import time
import threading
from collections import deque

from app.models.transfer import LOG_RETENTION_SECONDS

# 操作日志：按实例名分组存储，每条 {ts, msg, level}
_server_logs = {}
_server_logs_lock = threading.Lock()
_MAX_LOG_ENTRIES = 500  # 每个实例最多保留的日志条目数


def cleanup_old_logs():
    """清理超过 24 小时的操作日志（按实例分组）"""
    now = time.time()
    with _server_logs_lock:
        for server in list(_server_logs.keys()):
            entries = _server_logs[server]
            entries = deque(
                (e for e in entries if now - e["ts"] <= LOG_RETENTION_SECONDS),
                maxlen=_MAX_LOG_ENTRIES,
            )
            if entries:
                _server_logs[server] = entries
            else:
                del _server_logs[server]
