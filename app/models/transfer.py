import os
import time
import uuid
import threading
from datetime import datetime
from collections import deque

from app.models.keepalive import start_keepalive, stop_keepalive

# 传输任务全局存储: task_id -> task_info
_transfer_tasks = {}
_transfer_lock = threading.Lock()

# 每个服务器实例的传输锁：同一实例串行，不同实例并行
_server_locks = {}
_server_locks_guard = threading.Lock()

# 日志保留时长：24 小时
LOG_RETENTION_SECONDS = 24 * 3600
_MAX_TASKS = 200  # 最大任务条目数


def _get_server_lock(server_name):
    """获取或创建指定服务器的传输锁"""
    with _server_locks_guard:
        if server_name not in _server_locks:
            _server_locks[server_name] = threading.Lock()
        return _server_locks[server_name]


def _format_size(num_bytes):
    """格式化文件大小"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


class FTPManager:
    """FTP 连接和文件传输"""

    @staticmethod
    def test_connection(host, port, username, password):
        """测试 FTP 连接，返回 (success, message)"""
        from ftplib import FTP, error_perm, error_temp
        try:
            ftp = FTP()
            ftp.connect(host, int(port), timeout=10)
            ftp.login(username or "anonymous", password or "")
            ftp.close()
            return True, "连接成功"
        except error_perm:
            return False, "认证失败：用户名或密码错误"
        except error_temp:
            return False, "临时错误，请重试"
        except TimeoutError:
            return False, "连接超时，DBI 可能正在安装或已退出后端模式"
        except ConnectionRefusedError:
            return False, "连接被拒绝，DBI 可能已退出后端模式，请在 Switch 上重新打开 DBI → 后端模式"
        except (ConnectionResetError, BrokenPipeError):
            return False, "连接被重置，DBI 可能正在处理中或已退出后端模式"
        except OSError as e:
            if getattr(e, 'errno', None) in (54, 61, 111):
                return False, "连接被拒绝，DBI 可能已退出后端模式，请在 Switch 上重新打开 DBI → 后端模式"
            msg = str(e)
            if len(msg) > 80:
                msg = msg[:80] + "..."
            return False, msg
        except Exception as e:
            msg = str(e)
            if len(msg) > 80:
                msg = msg[:80] + "..."
            return False, msg

    @staticmethod
    def upload_files(task_id, server_info):
        """在后台线程中上传文件（仅上传 pending 状态的文件，支持重试重连）
        同一服务器实例串行，不同服务器实例并行。"""
        from ftplib import FTP, error_perm
        import socket
        task = _transfer_tasks[task_id]
        server_name = task.get("server", "")

        pending = [(idx, dict(f)) for idx, f in enumerate(task["files"]) if f["status"] == "pending"]
        total_bytes = sum(f["size"] for _, f in pending)
        task["total_bytes"] = total_bytes
        task["uploaded_bytes"] = 0
        task["log"] = deque(maxlen=100)

        server_lock = _get_server_lock(server_name)
        server_lock.acquire()
        try:
            stop_keepalive(server_name)
            if task.get("cancelled"):
                task["status"] = "cancelled"
                task["end_time"] = time.time()
                return

            task["status"] = "transferring"
            task["start_time"] = time.time()
            task["current_file"] = ""
            task["current_file_index"] = 0
            task["current_file_bytes"] = 0
            task["current_file_size"] = 0

            speed_samples = deque(maxlen=60)
            last_bytes = 0
            last_time = time.time()

            CONNECT_TIMEOUT = 15
            STALL_TIMEOUT = 300
            BLOCK_SIZE = 1048576
            KEEPALIVE_INTERVAL = 1.0
            MAX_RETRIES = 3

            def log(msg, level="info"):
                timestamp = datetime.now().strftime("%H:%M:%S")
                task["log"].append({"time": timestamp, "msg": msg, "level": level})

            def connect_ftp():
                f = FTP()
                f.connect(server_info["host"], int(server_info["port"]), timeout=CONNECT_TIMEOUT)
                f.login(server_info.get("username") or "anonymous",
                        server_info.get("password") or "")
                f.timeout = None
                if f.sock:
                    f.sock.settimeout(None)
                    f.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    if hasattr(socket, 'TCP_KEEPALIVE'):
                        f.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60)
                    elif hasattr(socket, 'TCP_KEEPIDLE'):
                        f.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
                upload_path = server_info.get("upload_path", "").strip()
                if upload_path:
                    for part in upload_path.strip("/").split("/"):
                        if part:
                            try:
                                f.cwd(part)
                            except error_perm:
                                try:
                                    f.mkd(part)
                                    f.cwd(part)
                                except error_perm:
                                    pass
                return f

            first_file = True
            ftp = None
            ftp_holder = [None]
            stall_event = threading.Event()
            wd_active = threading.Event()

            def watchdog():
                last_progress = -1
                stall_start = None
                while wd_active.is_set() and not stall_event.is_set():
                    time.sleep(5)
                    if not wd_active.is_set():
                        break
                    current = task.get("current_file_bytes", 0)
                    if current != last_progress:
                        last_progress = current
                        stall_start = None
                    else:
                        if stall_start is None:
                            stall_start = time.time()
                        elif time.time() - stall_start >= STALL_TIMEOUT:
                            log(f"传输停滞 {STALL_TIMEOUT} 秒，判定超时", "error")
                            stall_event.set()
                            try:
                                f_ref = ftp_holder[0]
                                if f_ref and f_ref.sock:
                                    f_ref.sock.shutdown(socket.SHUT_RDWR)
                                    f_ref.sock.close()
                            except Exception:
                                pass
                            break

            try:
                ftp = connect_ftp()
                ftp_holder[0] = ftp
                log(f"已连接到 {server_info['host']}:{server_info['port']}", "info")
                upload_path = server_info.get("upload_path", "").strip()
                if upload_path:
                    log(f"上传目录: {upload_path}", "info")

                for idx, finfo in pending:
                    if task.get("cancelled"):
                        log("传输已取消", "warning")
                        break

                    if task["files"][idx]["status"] != "pending":
                        continue

                    if not first_file:
                        try:
                            ftp.voidcmd("NOOP")
                        except Exception:
                            log("心跳检测失败，尝试重连...", "warning")
                            try:
                                ftp = connect_ftp()
                                ftp_holder[0] = ftp
                                log("重连成功", "success")
                            except Exception as e:
                                log(f"重连失败: {e}", "error")
                                task["files"][idx]["status"] = "failed"
                                task["files"][idx]["error"] = f"重连失败: {e}"
                                continue
                    first_file = False

                    fname = finfo["name"]
                    fpath = finfo["path"]
                    fsize = finfo["size"]
                    task["current_file"] = fname
                    task["current_file_index"] = idx
                    task["current_file_size"] = fsize
                    task["files"][idx]["uploaded_bytes"] = 0
                    task["files"][idx]["progress"] = 0
                    log(f"正在上传: {fname} ({_format_size(fsize)})", "info")

                    def callback(block, _task=task, _idx=idx, _fname=fname, _fsize=fsize):
                        _task["current_file_bytes"] += len(block)
                        _task["uploaded_bytes"] += len(block)
                        _task["files"][_idx]["uploaded_bytes"] = _task["current_file_bytes"]
                        _task["files"][_idx]["progress"] = round(_task["current_file_bytes"] / _fsize * 100, 1) if _fsize > 0 else 0
                        nonlocal last_bytes, last_time
                        now = time.time()
                        dt = now - last_time
                        if dt >= KEEPALIVE_INTERVAL:
                            speed = (_task["uploaded_bytes"] - last_bytes) / dt
                            speed_samples.append(speed)
                            last_bytes = _task["uploaded_bytes"]
                            last_time = now

                    file_done = False
                    uploaded_before_file = task["uploaded_bytes"]
                    for attempt in range(1, MAX_RETRIES + 1):
                        if task.get("cancelled"):
                            break
                        stall_event.clear()
                        wd_active.set()
                        task["uploaded_bytes"] = uploaded_before_file
                        task["current_file_bytes"] = 0
                        task["files"][idx]["status"] = "uploading"
                        task["files"][idx]["uploaded_bytes"] = 0
                        task["files"][idx]["progress"] = 0
                        wd_thread = threading.Thread(target=watchdog, daemon=True)
                        wd_thread.start()
                        try:
                            with open(fpath, "rb") as f:
                                ftp.storbinary(f"STOR {fname}", f, blocksize=BLOCK_SIZE, callback=callback)
                            if task["files"][idx]["status"] != "cancelled":
                                task["files"][idx]["status"] = "completed"
                                task["files"][idx]["progress"] = 100
                                log(f"完成: {fname}", "success")
                            else:
                                log(f"已取消: {fname}", "warning")
                            file_done = True
                            break
                        except Exception as e:
                            is_stall = stall_event.is_set()
                            if is_stall:
                                log(f"传输停滞超时: {fname} (第 {attempt}/{MAX_RETRIES} 次)", "warning")
                            else:
                                log(f"上传异常: {fname} - {e} (第 {attempt}/{MAX_RETRIES} 次)", "warning")
                            if attempt < MAX_RETRIES and not task.get("cancelled") and task["files"][idx]["status"] != "cancelled":
                                try:
                                    ftp = connect_ftp()
                                    ftp_holder[0] = ftp
                                    log(f"重连成功，重试 {fname}", "success")
                                except Exception as re_err:
                                    log(f"重连失败: {re_err}", "error")
                                    task["files"][idx]["status"] = "failed"
                                    task["files"][idx]["error"] = f"重连失败: {re_err}"
                                    break
                            else:
                                if task["files"][idx]["status"] != "cancelled":
                                    task["files"][idx]["status"] = "failed"
                                    task["files"][idx]["error"] = str(e) if not is_stall else f"传输停滞超时（300秒无进展）"
                                    log(f"上传失败: {fname} - {task['files'][idx]['error']}", "error")
                        finally:
                            wd_active.clear()
                            stall_event.clear()

                    if not file_done and not task.get("cancelled"):
                        try:
                            ftp = connect_ftp()
                            ftp_holder[0] = ftp
                            log("重连成功，继续后续文件", "success")
                        except Exception as re_err:
                            log(f"重连失败: {re_err}", "error")

                if task.get("cancelled"):
                    task["status"] = "cancelled"
                else:
                    task["status"] = "completed"
                    log("传输结束", "success")
                try:
                    ftp.close()
                except Exception:
                    pass
            except Exception as e:
                msg = str(e)
                log(f"传输错误: {msg}", "error")
                task["status"] = "error"
                task["error"] = msg
                for idx, _ in pending:
                    if task["files"][idx]["status"] == "pending":
                        task["files"][idx]["status"] = "failed"
                        task["files"][idx]["error"] = msg
                if ftp:
                    try:
                        ftp.close()
                    except Exception:
                        pass
            finally:
                task["end_time"] = time.time()
                if task.get("start_time") and task.get("end_time"):
                    elapsed = task["end_time"] - task["start_time"]
                    if elapsed > 0:
                        task["avg_speed"] = task["uploaded_bytes"] / elapsed
        finally:
            server_lock.release()
            if task.get("status") == "completed":
                start_keepalive(server_name)


def cleanup_old_tasks():
    """清理超过 24 小时的传输任务，限制总任务数"""
    now = time.time()
    with _transfer_lock:
        to_remove = [
            tid for tid, task in _transfer_tasks.items()
            if task.get("end_time") and (now - task["end_time"]) > LOG_RETENTION_SECONDS
        ]
        for tid in to_remove:
            del _transfer_tasks[tid]
        if len(_transfer_tasks) > _MAX_TASKS:
            terminal = [
                (tid, task.get("end_time", 0))
                for tid, task in _transfer_tasks.items()
                if task.get("status") in ("completed", "error", "cancelled")
            ]
            terminal.sort(key=lambda x: x[1])
            excess = len(_transfer_tasks) - _MAX_TASKS
            for tid, _ in terminal[:excess]:
                del _transfer_tasks[tid]
