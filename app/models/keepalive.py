import time
import threading

from app.models.config import ConfigManager

# FTP 保活连接：每个实例独立后台保活，周期性发送 NOOP
_keepalive_stops = {}        # server_name -> threading.Event
_keepalive_threads = {}      # server_name -> threading.Thread
_keepalive_guard = threading.Lock()
KEEPALIVE_INTERVAL = 30      # NOOP 发送间隔（秒）
KEEPALIVE_MAX_LIFETIME = None  # None = 持续保活；设为秒数则到期自动停止
KEEPALIVE_RETRY_BACKOFF = [10, 20, 40, 60]  # 连接失败重试间隔（秒）


def _get_server_info(server_name):
    """从配置中获取服务器信息"""
    try:
        cfg = ConfigManager.load()
        for s in cfg.get("ftp_servers", []):
            if s.get("name") == server_name:
                return dict(s)
    except Exception:
        pass
    return None


def start_keepalive(server_name):
    """对指定实例启动 FTP 保活连接（传输完成后调用）。

    保活线程建立独立 FTP 连接，周期性发送 NOOP 维持连接，
    让 DBI 知道有活动客户端，避免无活动连接时退出 FTP 后端模式。
    传输开始前会调用 stop_keepalive 停止保活。
    """
    server_info = _get_server_info(server_name)
    if not server_info:
        return

    with _keepalive_guard:
        if server_name in _keepalive_threads and _keepalive_threads[server_name].is_alive():
            return
        stop_event = threading.Event()
        _keepalive_stops[server_name] = stop_event

    def _keepalive_loop():
        import socket
        from ftplib import FTP
        start_ts = time.time()
        ftp = None
        consecutive_fail = 0
        while not stop_event.is_set():
            if KEEPALIVE_MAX_LIFETIME is not None and time.time() - start_ts > KEEPALIVE_MAX_LIFETIME:
                break
            if ftp is None:
                try:
                    ftp = FTP()
                    ftp.connect(server_info["host"], int(server_info["port"]), timeout=10)
                    ftp.login(server_info.get("username") or "anonymous",
                              server_info.get("password") or "")
                    if ftp.sock:
                        ftp.sock.settimeout(None)
                        ftp.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    consecutive_fail = 0
                except Exception:
                    ftp = None
                    consecutive_fail += 1
                    idx = min(consecutive_fail - 1, len(KEEPALIVE_RETRY_BACKOFF) - 1)
                    wait_sec = KEEPALIVE_RETRY_BACKOFF[idx]
                    stop_event.wait(wait_sec)
                    continue
            try:
                ftp.voidcmd("NOOP")
                consecutive_fail = 0
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass
                ftp = None
                consecutive_fail += 1
            stop_event.wait(KEEPALIVE_INTERVAL)
        if ftp is not None:
            try:
                ftp.close()
            except Exception:
                pass
        with _keepalive_guard:
            _keepalive_threads.pop(server_name, None)
            _keepalive_stops.pop(server_name, None)

    t = threading.Thread(target=_keepalive_loop, name=f"ftp-keepalive-{server_name}", daemon=True)
    _keepalive_threads[server_name] = t
    t.start()


def stop_keepalive(server_name):
    """停止指定实例的 FTP 保活连接（传输开始前调用）"""
    with _keepalive_guard:
        stop_event = _keepalive_stops.get(server_name)
        if stop_event:
            stop_event.set()


def start_all_keepalive():
    """为配置中的所有 FTP 服务器逐一启动保活连接（服务启动时调用）"""
    try:
        cfg = ConfigManager.load()
        servers = cfg.get("ftp_servers", [])
    except Exception:
        return
    for s in servers:
        name = s.get("name")
        if name:
            start_keepalive(name)
