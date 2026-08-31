import time
import threading

from app.models.transfer import FTPManager

# FTP 连接状态缓存: server_name -> {status, message, timestamp}
_ftp_status = {}
_ftp_status_lock = threading.Lock()

# FTP 连接测试线程状态: server_name -> "idle"/"running"
_ftp_test_threads = {}
_ftp_test_threads_lock = threading.Lock()


def _run_ftp_test_async(name, server_info):
    """后台线程执行 FTP 连接测试，结果写入 _ftp_status 缓存"""
    with _ftp_test_threads_lock:
        if _ftp_test_threads.get(name) == "running":
            return
        _ftp_test_threads[name] = "running"
    try:
        success, message = FTPManager.test_connection(
            server_info["host"], server_info["port"],
            server_info.get("username", ""), server_info.get("password", "")
        )
        with _ftp_status_lock:
            _ftp_status[name] = {
                "status": "connected" if success else "error",
                "message": message,
                "timestamp": time.time(),
            }
    finally:
        with _ftp_test_threads_lock:
            _ftp_test_threads[name] = "idle"
