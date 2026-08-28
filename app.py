#!/usr/bin/env python3
"""
Switch DBI FTP 传输工具 - 后端服务
自动扫描目录下的 Switch 安装包，通过 FTP 发送到 Switch 上的 DBI 后端。
"""

import os
import io
import time
import threading
import uuid
from pathlib import Path
from datetime import datetime
from collections import deque

import yaml
from flask import Flask, request, jsonify, render_template, send_from_directory

app = Flask(__name__, template_folder="templates", static_folder="static")

# 全局配置文件路径
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml")

# Switch 安装包默认扩展名
DEFAULT_EXTENSIONS = [".nsp", ".nsz", ".xci", ".xcz"]

# 传输任务全局存储: task_id -> task_info
_transfer_tasks = {}
_transfer_lock = threading.Lock()

# FTP 连接状态缓存: server_name -> {status, message, timestamp}
_ftp_status = {}
_ftp_status_lock = threading.Lock()


# ============================================================
# 配置管理
# ============================================================

class ConfigManager:
    """加载和保存 config.yml"""

    @staticmethod
    def load():
        if not os.path.exists(CONFIG_PATH):
            return ConfigManager._default_config()
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return ConfigManager._merge_defaults(cfg)

    @staticmethod
    def save(cfg):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _default_config():
        return {
            "server": {"host": "0.0.0.0", "port": 8090},
            "scan_dirs": [],
            "scan_extensions": DEFAULT_EXTENSIONS,
            "scan_interval": 300,
            "ftp_servers": [],
            "ui_settings": {"page_size": 10, "show_all_files": False, "language": "zh"},
        }

    @staticmethod
    def _merge_defaults(cfg):
        defaults = ConfigManager._default_config()
        for key in defaults:
            if key not in cfg:
                cfg[key] = defaults[key]
        return cfg


# ============================================================
# 文件扫描
# ============================================================

class FileScanner:
    """扫描目录下所有 Switch 安装包"""

    # 显示所有文件时排除的系统/隐藏文件
    _SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".gitkeep"}

    @staticmethod
    def scan(scan_dirs, extensions=None, show_all=False):
        if extensions is None:
            extensions = DEFAULT_EXTENSIONS
        ext_set = {e.lower() for e in extensions}
        results = []
        for scan_dir in scan_dirs:
            scan_dir = os.path.expanduser(scan_dir)
            if not os.path.isdir(scan_dir):
                continue
            for root, dirs, files in os.walk(scan_dir):
                for fname in files:
                    # 跳过隐藏文件和系统文件
                    if fname.startswith(".") or fname.lower() in FileScanner._SKIP_NAMES:
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    if not show_all and ext not in ext_set:
                        continue
                    fpath = os.path.join(root, fname)
                    try:
                        fsize = os.path.getsize(fpath)
                        mtime = os.path.getmtime(fpath)
                    except OSError:
                        continue
                    results.append({
                        "name": fname,
                        "path": fpath,
                        "dir": root,
                        "size": fsize,
                        "mtime": mtime,
                        "mtime_str": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                        "ext": ext,
                    })
        # 按修改时间倒序（最新在前）
        results.sort(key=lambda x: x["mtime"], reverse=True)
        return results


# ============================================================
# 文件扫描管理（定时刷新 + 单任务锁）
# ============================================================

class FileScanManager:
    """管理文件扫描缓存，定时后台刷新，同一时间只允许一个扫描任务"""

    _instance = None
    _init_lock = threading.Lock()

    def __init__(self):
        self._scan_lock = threading.Lock()
        self._scanning = False
        self._cached_files = []
        self._cached_all_files = []
        self._last_scan_time = 0
        self._stop_event = threading.Event()
        self._thread = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def start_auto_scan(self, interval=60):
        """启动定时后台扫描线程"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._auto_loop, args=(interval,), daemon=True)
        self._thread.start()

    def stop_auto_scan(self):
        """停止定时扫描"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _auto_loop(self, interval):
        """定时扫描循环"""
        while not self._stop_event.is_set():
            try:
                self.do_scan()
            except Exception as e:
                print(f"[FileScanManager] auto scan error: {e}")
            self._stop_event.wait(interval)

    def do_scan(self):
        """执行一次扫描，如果已有扫描在运行则返回 False"""
        if not self._scan_lock.acquire(blocking=False):
            return False
        try:
            self._scanning = True
            cfg = ConfigManager.load()
            scan_dirs = cfg.get("scan_dirs", [])
            exts = cfg.get("scan_extensions", DEFAULT_EXTENSIONS)
            self._cached_files = FileScanner.scan(scan_dirs, exts, show_all=False)
            self._cached_all_files = FileScanner.scan(scan_dirs, exts, show_all=True)
            self._last_scan_time = time.time()
            return True
        finally:
            self._scanning = False
            self._scan_lock.release()

    @property
    def is_scanning(self):
        return self._scanning

    @property
    def last_scan_time(self):
        return self._last_scan_time

    def get_files(self, show_all=False):
        return self._cached_all_files if show_all else self._cached_files


# ============================================================
# FTP 传输管理
# ============================================================

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
            ftp.quit()
            return True, "连接成功"
        except error_perm:
            return False, "认证失败：用户名或密码错误"
        except error_temp:
            return False, "临时错误，请重试"
        except TimeoutError:
            return False, "连接超时"
        except ConnectionRefusedError:
            return False, "连接被拒绝，检查地址和端口"
        except Exception as e:
            msg = str(e)
            if len(msg) > 80:
                msg = msg[:80] + "..."
            return False, msg

    @staticmethod
    def upload_files(task_id, server_info):
        """在后台线程中上传文件（仅上传 pending 状态的文件，支持重试重连）"""
        from ftplib import FTP, error_perm
        import socket
        task = _transfer_tasks[task_id]

        # 收集所有 pending 状态的文件及其索引
        pending = [(idx, dict(f)) for idx, f in enumerate(task["files"]) if f["status"] == "pending"]
        total_bytes = sum(f["size"] for _, f in pending)
        task["total_bytes"] = total_bytes
        task["uploaded_bytes"] = 0
        task["status"] = "transferring"
        task["start_time"] = time.time()
        task["current_file"] = ""
        task["current_file_index"] = 0
        task["current_file_bytes"] = 0
        task["current_file_size"] = 0
        task["log"] = []

        # 速度计算 - 滑动窗口（60 个采样点 × 0.5s = 30 秒窗口）
        speed_samples = deque(maxlen=60)
        last_bytes = 0
        last_time = time.time()

        # 超时保护常量
        CONNECT_TIMEOUT = 15
        TRANSFER_TIMEOUT = 300       # 单次读写超时 5 分钟，支持大文件长时间传输
        BLOCK_SIZE = 1048576         # 1MB 块，提升大文件吞吐量
        KEEPALIVE_INTERVAL = 1.0     # 进度采样间隔（秒）

        def log(msg, level="info"):
            timestamp = datetime.now().strftime("%H:%M:%S")
            task["log"].append({"time": timestamp, "msg": msg, "level": level})

        def connect_ftp():
            """建立 FTP 连接并进入上传目录"""
            f = FTP()
            f.connect(server_info["host"], int(server_info["port"]), timeout=CONNECT_TIMEOUT)
            f.login(server_info.get("username") or "anonymous",
                    server_info.get("password") or "")
            if f.sock:
                # 启用 TCP keepalive，防止长时间传输时连接被路由/防火墙断开
                f.sock.settimeout(TRANSFER_TIMEOUT)
                f.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # macOS: keepalive 间隔 60 秒，探测 3 次
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

        try:
            ftp = connect_ftp()
            log(f"已连接到 {server_info['host']}:{server_info['port']}", "info")
            upload_path = server_info.get("upload_path", "").strip()
            if upload_path:
                log(f"上传目录: {upload_path}", "info")

            for idx, finfo in pending:
                if task.get("cancelled"):
                    log("传输已取消", "warning")
                    break

                # 只上传 pending 状态的文件
                if task["files"][idx]["status"] != "pending":
                    continue

                # 文件之间发送 NOOP 心跳，失败则重连
                if not first_file:
                    try:
                        ftp.voidcmd("NOOP")
                    except Exception:
                        log("心跳检测失败，尝试重连...", "warning")
                        try:
                            ftp = connect_ftp()
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
                task["current_file_bytes"] = 0
                task["current_file_size"] = fsize
                task["files"][idx]["status"] = "uploading"
                task["files"][idx]["uploaded_bytes"] = 0
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

                try:
                    with open(fpath, "rb") as f:
                        # STOR 默认覆盖已存在的同名文件（FTP 行为）
                        ftp.storbinary(f"STOR {fname}", f, blocksize=BLOCK_SIZE, callback=callback)
                    # STOR 完成后，检查文件是否已被用户取消（不覆盖 cancelled 状态）
                    if task["files"][idx]["status"] != "cancelled":
                        task["files"][idx]["status"] = "completed"
                        task["files"][idx]["progress"] = 100
                        log(f"完成: {fname}", "success")
                    else:
                        log(f"已取消: {fname}", "warning")
                except Exception as e:
                    # 失败时不覆盖已取消的文件
                    if task["files"][idx]["status"] != "cancelled":
                        task["files"][idx]["status"] = "failed"
                        task["files"][idx]["error"] = str(e)
                        log(f"上传失败: {fname} - {e}", "error")
                    # 重连以继续后续文件
                    try:
                        ftp = connect_ftp()
                        log("重连成功，继续后续文件", "success")
                    except Exception as re_err:
                        log(f"重连失败: {re_err}", "error")

            if task.get("cancelled"):
                task["status"] = "cancelled"
            else:
                task["status"] = "completed"
                log("传输结束", "success")
            try:
                ftp.quit()
            except Exception:
                pass
        except Exception as e:
            msg = str(e)
            log(f"传输错误: {msg}", "error")
            task["status"] = "error"
            task["error"] = msg
            # 将剩余 pending 文件标记为失败（可重试）
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


def _format_size(num_bytes):
    """格式化文件大小"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


# ============================================================
# 日志保留时长：24 小时
LOG_RETENTION_SECONDS = 24 * 3600


def cleanup_old_tasks():
    """清理超过 24 小时的传输任务及其日志"""
    now = time.time()
    with _transfer_lock:
        to_remove = [
            tid for tid, task in _transfer_tasks.items()
            if task.get("end_time") and (now - task["end_time"]) > LOG_RETENTION_SECONDS
        ]
        for tid in to_remove:
            del _transfer_tasks[tid]


# ============================================================
# API 路由
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


def _mask_server(s):
    """返回不包含明文密码的服务器信息"""
    s_copy = dict(s)
    if s_copy.get("password"):
        s_copy["password_masked"] = "•" * len(s_copy["password"])
        s_copy["has_password"] = True
    else:
        s_copy["has_password"] = False
    del s_copy["password"]
    return s_copy


@app.route("/api/config")
def get_config():
    cfg = ConfigManager.load()
    servers = [_mask_server(s) for s in cfg.get("ftp_servers", [])]
    return jsonify({
        "scan_dirs": cfg.get("scan_dirs", []),
        "scan_extensions": cfg.get("scan_extensions", DEFAULT_EXTENSIONS),
        "ftp_servers": servers,
    })


@app.route("/api/files")
def list_files():
    cleanup_old_tasks()
    show_all = request.args.get("all", "false").lower() == "true"
    mgr = FileScanManager.get()
    files = mgr.get_files(show_all=show_all)
    return jsonify({
        "files": files,
        "total": len(files),
        "scanning": mgr.is_scanning,
        "last_scan_time": mgr.last_scan_time,
    })


@app.route("/api/files/scan", methods=["POST"])
def rescan_files():
    cleanup_old_tasks()
    mgr = FileScanManager.get()
    # 如果已有扫描任务在运行，返回提示
    if mgr.is_scanning:
        return jsonify({
            "scanning": True,
            "message": "刷新任务进行中",
            "total": len(mgr.get_files()),
        })
    # 执行扫描（同步等待完成）
    mgr.do_scan()
    show_all = request.args.get("all", "false").lower() == "true"
    files = mgr.get_files(show_all=show_all)
    return jsonify({
        "files": files,
        "total": len(files),
        "scanning": False,
        "last_scan_time": mgr.last_scan_time,
    })


@app.route("/api/files/scan-status")
def scan_status():
    mgr = FileScanManager.get()
    return jsonify({
        "scanning": mgr.is_scanning,
        "last_scan_time": mgr.last_scan_time,
    })


@app.route("/api/scan-dirs", methods=["GET"])
def list_scan_dirs():
    cfg = ConfigManager.load()
    return jsonify({"scan_dirs": cfg.get("scan_dirs", [])})


@app.route("/api/scan-dirs", methods=["POST"])
def add_scan_dir():
    data = request.json
    if not data or not data.get("path"):
        return jsonify({"error": "路径不能为空"}), 400
    path = data["path"].strip()
    cfg = ConfigManager.load()
    scan_dirs = cfg.get("scan_dirs", [])
    if path in scan_dirs:
        return jsonify({"error": "路径已存在"}), 400
    scan_dirs.append(path)
    cfg["scan_dirs"] = scan_dirs
    ConfigManager.save(cfg)
    return jsonify({"scan_dirs": scan_dirs})


@app.route("/api/scan-dirs", methods=["DELETE"])
def remove_scan_dir():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "路径不能为空"}), 400
    cfg = ConfigManager.load()
    scan_dirs = cfg.get("scan_dirs", [])
    scan_dirs = [d for d in scan_dirs if d != path]
    cfg["scan_dirs"] = scan_dirs
    ConfigManager.save(cfg)
    return jsonify({"scan_dirs": scan_dirs})


@app.route("/api/ui-settings", methods=["GET"])
def get_ui_settings():
    cfg = ConfigManager.load()
    defaults = ConfigManager._default_config()["ui_settings"]
    settings = {**defaults, **cfg.get("ui_settings", {})}
    return jsonify(settings)


@app.route("/api/ui-settings", methods=["POST"])
def save_ui_settings():
    data = request.json or {}
    cfg = ConfigManager.load()
    current = cfg.get("ui_settings", {})
    current.update(data)
    cfg["ui_settings"] = current
    ConfigManager.save(cfg)
    return jsonify({"ok": True, "ui_settings": current})


@app.route("/api/servers", methods=["GET"])
def list_servers():
    cfg = ConfigManager.load()
    servers = [_mask_server(s) for s in cfg.get("ftp_servers", [])]
    return jsonify({"servers": servers})


@app.route("/api/servers", methods=["POST"])
def add_server():
    data = request.json
    if not data or not data.get("name") or not data.get("host"):
        return jsonify({"error": "名称和地址不能为空"}), 400

    cfg = ConfigManager.load()
    servers = cfg.setdefault("ftp_servers", [])

    # 检查重名
    for s in servers:
        if s["name"] == data["name"]:
            return jsonify({"error": f"服务器名称 '{data['name']}' 已存在"}), 400

    new_server = {
        "name": data["name"],
        "host": data["host"],
        "port": int(data.get("port", 5000)),
        "username": data.get("username", "ftp"),
        "password": data.get("password", ""),
        "upload_path": data.get("upload_path", ""),
    }
    servers.append(new_server)
    ConfigManager.save(cfg)
    return jsonify({"ok": True, "server": new_server})


@app.route("/api/servers/<name>", methods=["DELETE"])
def delete_server(name):
    cfg = ConfigManager.load()
    servers = cfg.get("ftp_servers", [])
    before = len(servers)
    cfg["ftp_servers"] = [s for s in servers if s["name"] != name]
    if len(cfg["ftp_servers"]) == before:
        return jsonify({"error": f"未找到服务器 '{name}'"}), 404
    ConfigManager.save(cfg)
    return jsonify({"ok": True})


@app.route("/api/servers/<name>/status", methods=["GET"])
def server_status(name):
    cfg = ConfigManager.load()
    server = None
    for s in cfg.get("ftp_servers", []):
        if s["name"] == name:
            server = s
            break
    if not server:
        return jsonify({"status": "unknown", "message": "服务器不存在"}), 404

    success, message = FTPManager.test_connection(
        server["host"], server["port"],
        server.get("username", ""), server.get("password", "")
    )
    with _ftp_status_lock:
        _ftp_status[name] = {
            "status": "connected" if success else "error",
            "message": message,
            "timestamp": time.time(),
        }
    return jsonify({
        "status": "connected" if success else "error",
        "message": message,
    })


@app.route("/api/transfer", methods=["POST"])
def start_transfer():
    cleanup_old_tasks()
    data = request.json
    if not data:
        return jsonify({"error": "请求数据为空"}), 400

    server_name = data.get("server")
    files = data.get("files", [])
    if not server_name:
        return jsonify({"error": "请选择 FTP 服务器"}), 400
    if not files:
        return jsonify({"error": "请选择要发送的文件"}), 400

    cfg = ConfigManager.load()
    server_info = None
    for s in cfg.get("ftp_servers", []):
        if s["name"] == server_name:
            server_info = s
            break
    if not server_info:
        return jsonify({"error": f"未找到服务器 '{server_name}'"}), 404

    # 验证文件是否存在
    file_list = []
    for f in files:
        fpath = f.get("path", "")
        if os.path.isfile(fpath):
            try:
                fsize = os.path.getsize(fpath)
                mtime = os.path.getmtime(fpath)
            except OSError:
                continue
            file_list.append({
                "name": f.get("name", os.path.basename(fpath)),
                "path": fpath,
                "size": fsize,
                "mtime": mtime,
            })

    if not file_list:
        return jsonify({"error": "所选文件均不存在"}), 400

    # 过滤掉已在上传列表中的文件（pending/uploading/failed 状态）
    with _transfer_lock:
        existing_paths = set()
        for t in _transfer_tasks.values():
            for f in t.get("files", []):
                if f["status"] in ("pending", "uploading", "failed"):
                    existing_paths.add(f["path"])
    skipped = [f for f in file_list if f["path"] in existing_paths]
    file_list = [f for f in file_list if f["path"] not in existing_paths]

    if not file_list:
        return jsonify({
            "error": "所有文件已在上传列表中，已自动忽略",
            "skipped": len(skipped),
        }), 400

    task_id = str(uuid.uuid4())[:8]
    task = {
        "id": task_id,
        "status": "starting",
        "total_files": len(file_list),
        "total_bytes": sum(f["size"] for f in file_list),
        "uploaded_bytes": 0,
        "current_file": "",
        "current_file_index": 0,
        "current_file_bytes": 0,
        "current_file_size": 0,
        "log": [],
        "server": server_name,
        "server_host": f"{server_info['host']}:{server_info['port']}",
        "cancelled": False,
        "start_time": None,
        "end_time": None,
        "avg_speed": 0,
        "files": [
            {
                "name": f["name"],
                "path": f["path"],
                "size": f["size"],
                "status": "pending",  # pending, uploading, completed, failed, cancelled
                "uploaded_bytes": 0,
                "progress": 0,
                "error": None,
            }
            for f in file_list
        ],
    }
    with _transfer_lock:
        _transfer_tasks[task_id] = task

    # 启动后台传输线程
    thread = threading.Thread(
        target=FTPManager.upload_files,
        args=(task_id, server_info),
        daemon=True,
    )
    thread.start()

    result = {"task_id": task_id, "total_files": len(file_list),
              "total_bytes": task["total_bytes"]}
    if skipped:
        result["skipped"] = len(skipped)
    return jsonify(result)


@app.route("/api/transfer/<task_id>/status", methods=["GET"])
def transfer_status(task_id):
    with _transfer_lock:
        task = _transfer_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    # 计算实时速度
    speed = 0
    if task.get("start_time") and task["status"] == "transferring":
        elapsed = time.time() - task["start_time"]
        if elapsed > 0 and task["uploaded_bytes"] > 0:
            speed = task["uploaded_bytes"] / elapsed

    # 计算进度百分比
    progress = 0
    if task["total_bytes"] > 0:
        progress = (task["uploaded_bytes"] / task["total_bytes"]) * 100

    # 计算ETA
    eta = "计算中..."
    if speed > 0 and task["status"] == "transferring":
        remaining = task["total_bytes"] - task["uploaded_bytes"]
        eta_seconds = remaining / speed
        if eta_seconds < 60:
            eta = f"{int(eta_seconds)}秒"
        else:
            eta = f"{int(eta_seconds / 60)}分{int(eta_seconds % 60)}秒"

    return jsonify({
        "id": task_id,
        "status": task["status"],
        "total_files": task["total_files"],
        "total_bytes": task["total_bytes"],
        "uploaded_bytes": task["uploaded_bytes"],
        "progress": round(progress, 1),
        "speed": round(speed, 0),
        "speed_str": _format_size(speed) + "/s" if speed > 0 else "0 B/s",
        "eta": eta,
        "current_file": task["current_file"],
        "current_file_index": task["current_file_index"],
        "current_file_bytes": task["current_file_bytes"],
        "current_file_size": task["current_file_size"],
        "current_file_progress": round(
            (task["current_file_bytes"] / task["current_file_size"] * 100), 1
        ) if task["current_file_size"] > 0 else 0,
        "log": task["log"][-50:],
        "avg_speed": task.get("avg_speed", 0),
        "error": task.get("error", ""),
        "files": task.get("files", []),
    })


@app.route("/api/transfer/<task_id>/cancel", methods=["POST"])
def cancel_transfer(task_id):
    with _transfer_lock:
        task = _transfer_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    task["cancelled"] = True
    return jsonify({"ok": True})


@app.route("/api/transfer/<task_id>", methods=["DELETE"])
def delete_transfer(task_id):
    with _transfer_lock:
        if task_id in _transfer_tasks:
            del _transfer_tasks[task_id]
            return jsonify({"ok": True})
    return jsonify({"error": "任务不存在"}), 404


@app.route("/api/transfers")
def list_transfers():
    cleanup_old_tasks()
    with _transfer_lock:
        tasks = []
        for tid, task in _transfer_tasks.items():
            tasks.append({
                "id": tid,
                "status": task["status"],
                "server": task.get("server", ""),
                "server_host": task.get("server_host", ""),
                "total_files": task["total_files"],
                "total_bytes": task["total_bytes"],
                "uploaded_bytes": task["uploaded_bytes"],
                "current_file": task["current_file"],
                "current_file_index": task["current_file_index"],
                "start_time": task.get("start_time"),
                "end_time": task.get("end_time"),
                "files": task.get("files", []),
            })
    # Sort: active first, then by start_time descending
    active_statuses = {"starting", "transferring"}
    tasks.sort(key=lambda t: (
        0 if t["status"] in active_statuses else 1,
        -(t.get("start_time") or 0)
    ))
    return jsonify({"transfers": tasks})


@app.route("/api/transfer/<task_id>/cancel-files", methods=["POST"])
def cancel_files(task_id):
    data = request.json or {}
    file_indices = data.get("file_indices", [])
    with _transfer_lock:
        task = _transfer_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    cancelled_count = 0
    for idx in file_indices:
        if 0 <= idx < len(task.get("files", [])):
            if task["files"][idx]["status"] in ("pending", "uploading", "failed"):
                task["files"][idx]["status"] = "cancelled"
                cancelled_count += 1
    return jsonify({"ok": True, "cancelled": cancelled_count})


@app.route("/api/transfer/<task_id>/retry-files", methods=["POST"])
def retry_files(task_id):
    """重试选中的失败文件"""
    data = request.json or {}
    file_indices = data.get("file_indices", [])
    with _transfer_lock:
        task = _transfer_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    # 任务必须在终止状态才能重试
    if task["status"] not in ("completed", "error", "cancelled"):
        return jsonify({"error": "任务仍在运行中，无法重试"}), 400

    # 重置选中的失败文件为 pending
    reset_count = 0
    for idx in file_indices:
        if 0 <= idx < len(task.get("files", [])):
            if task["files"][idx]["status"] == "failed":
                task["files"][idx]["status"] = "pending"
                task["files"][idx]["error"] = None
                task["files"][idx]["progress"] = 0
                task["files"][idx]["uploaded_bytes"] = 0
                reset_count += 1

    if reset_count == 0:
        return jsonify({"error": "没有可重试的失败文件"}), 400

    # 获取服务器配置
    cfg = ConfigManager.load()
    server_info = None
    for s in cfg.get("ftp_servers", []):
        if s["name"] == task.get("server"):
            server_info = s
            break
    if not server_info:
        return jsonify({"error": "服务器配置不存在"}), 400

    # 重置任务状态并启动新线程
    task["status"] = "transferring"
    task["cancelled"] = False
    task["start_time"] = None
    task["end_time"] = None
    task["error"] = None

    thread = threading.Thread(
        target=FTPManager.upload_files,
        args=(task_id, server_info),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "retried": reset_count})


if __name__ == "__main__":
    cfg = ConfigManager.load()
    host = os.environ.get("HOST", cfg.get("server", {}).get("host", "0.0.0.0"))
    port = int(os.environ.get("PORT", cfg.get("server", {}).get("port", 8090)))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"

    # 启动时执行初始扫描
    scan_mgr = FileScanManager.get()
    scan_mgr.do_scan()
    print(f"初始扫描完成: {len(scan_mgr.get_files())} 个安装包")

    # 启动定时后台扫描（默认 300 秒）
    scan_interval = int(cfg.get("scan_interval", 300))
    scan_mgr.start_auto_scan(scan_interval)
    print(f"定时扫描已启动: 每 {scan_interval} 秒刷新一次")

    print(f"Switch DBI FTP 传输工具启动中...")
    print(f"访问地址: http://localhost:{port}")
    print(f"扫描目录: {cfg.get('scan_dirs', [])}")
    print(f"FTP 服务器: {[s['name'] for s in cfg.get('ftp_servers', [])]}")
    app.run(host=host, port=port, debug=debug)
