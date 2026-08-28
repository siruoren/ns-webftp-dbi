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
            "ftp_servers": [],
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

    @staticmethod
    def scan(scan_dirs, extensions=None):
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
                    if os.path.splitext(fname)[1].lower() in ext_set:
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
                        })
        # 按修改时间倒序（最新在前）
        results.sort(key=lambda x: x["mtime"], reverse=True)
        return results


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
    def upload_files(task_id, server_info, file_list):
        """在后台线程中上传文件"""
        from ftplib import FTP, error_perm
        task = _transfer_tasks[task_id]
        total_bytes = sum(f["size"] for f in file_list)
        task["total_bytes"] = total_bytes
        task["uploaded_bytes"] = 0
        task["status"] = "transferring"
        task["start_time"] = time.time()
        task["current_file"] = ""
        task["current_file_index"] = 0
        task["current_file_bytes"] = 0
        task["current_file_size"] = 0
        task["log"] = []

        # 速度计算 - 滑动窗口
        speed_samples = deque(maxlen=30)  # 最近 30 个采样点
        last_bytes = 0
        last_time = time.time()

        def log(msg, level="info"):
            timestamp = datetime.now().strftime("%H:%M:%S")
            task["log"].append({"time": timestamp, "msg": msg, "level": level})

        try:
            ftp = FTP()
            ftp.connect(server_info["host"], int(server_info["port"]), timeout=15)
            ftp.login(server_info.get("username") or "anonymous",
                      server_info.get("password") or "")
            log(f"已连接到 {server_info['host']}:{server_info['port']}", "info")

            upload_path = server_info.get("upload_path", "").strip()
            if upload_path:
                # 尝试进入目标目录，不存在则创建
                for part in upload_path.strip("/").split("/"):
                    if part:
                        try:
                            ftp.cwd(part)
                        except error_perm:
                            try:
                                ftp.mkd(part)
                                ftp.cwd(part)
                            except error_perm:
                                pass
                log(f"上传目录: {upload_path}", "info")

            for idx, finfo in enumerate(file_list):
                if task.get("cancelled"):
                    log("传输已取消", "warning")
                    break

                fname = finfo["name"]
                fpath = finfo["path"]
                fsize = finfo["size"]
                task["current_file"] = fname
                task["current_file_index"] = idx
                task["current_file_bytes"] = 0
                task["current_file_size"] = fsize
                log(f"正在上传: {fname} ({_format_size(fsize)})", "info")

                # 使用回调跟踪进度
                block_size = 65536  # 64KB 块

                def callback(block, _task=task, _fname=fname, _fsize=fsize):
                    _task["current_file_bytes"] += len(block)
                    _task["uploaded_bytes"] += len(block)
                    # 速度采样
                    nonlocal last_bytes, last_time
                    now = time.time()
                    dt = now - last_time
                    if dt >= 0.5:
                        speed = (_task["uploaded_bytes"] - last_bytes) / dt
                        speed_samples.append(speed)
                        last_bytes = _task["uploaded_bytes"]
                        last_time = now

                with open(fpath, "rb") as f:
                    ftp.storbinary(f"STOR {fname}", f, blocksize=block_size, callback=callback)

                if not task.get("cancelled"):
                    log(f"完成: {fname}", "success")
                task["current_file_index"] = idx + 1

            if task.get("cancelled"):
                task["status"] = "cancelled"
            else:
                task["status"] = "completed"
                log("所有文件传输完成！", "success")
            ftp.quit()
        except Exception as e:
            msg = str(e)
            log(f"传输错误: {msg}", "error")
            task["status"] = "error"
            task["error"] = msg
            try:
                ftp.quit()
            except Exception:
                pass
        finally:
            task["end_time"] = time.time()

        # 计算平均速度
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
    cfg = ConfigManager.load()
    files = FileScanner.scan(cfg.get("scan_dirs", []), cfg.get("scan_extensions"))
    return jsonify({"files": files, "total": len(files)})


@app.route("/api/files/scan", methods=["POST"])
def rescan_files():
    cfg = ConfigManager.load()
    files = FileScanner.scan(cfg.get("scan_dirs", []), cfg.get("scan_extensions"))
    return jsonify({"files": files, "total": len(files)})


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
        "cancelled": False,
        "start_time": None,
        "end_time": None,
        "avg_speed": 0,
    }
    with _transfer_lock:
        _transfer_tasks[task_id] = task

    # 启动后台传输线程
    thread = threading.Thread(
        target=FTPManager.upload_files,
        args=(task_id, server_info, file_list),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id, "total_files": len(file_list),
                     "total_bytes": task["total_bytes"]})


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


if __name__ == "__main__":
    cfg = ConfigManager.load()
    host = cfg.get("server", {}).get("host", "0.0.0.0")
    port = cfg.get("server", {}).get("port", 8090)
    print(f"Switch DBI FTP 传输工具启动中...")
    print(f"访问地址: http://localhost:{port}")
    print(f"扫描目录: {cfg.get('scan_dirs', [])}")
    print(f"FTP 服务器: {[s['name'] for s in cfg.get('ftp_servers', [])]}")
    app.run(host=host, port=port, debug=True)
