import time
import threading
from flask import Blueprint, request, jsonify

from app.models.config import ConfigManager, DEFAULT_EXTENSIONS, mask_server
from app.models.keepalive import (
    start_keepalive, stop_keepalive,
    _keepalive_threads, _keepalive_stops, _keepalive_guard,
)
from app.models.ftp_status import (
    _ftp_status, _ftp_status_lock,
    _run_ftp_test_async,
)
from app.models.transfer import _server_locks, _server_locks_guard

bp = Blueprint("servers", __name__)


@bp.route("/api/config")
def get_config():
    cfg = ConfigManager.load()
    servers = [mask_server(s) for s in cfg.get("ftp_servers", [])]
    return jsonify({
        "scan_dirs": cfg.get("scan_dirs", []),
        "scan_extensions": cfg.get("scan_extensions", DEFAULT_EXTENSIONS),
        "ftp_servers": servers,
    })


@bp.route("/api/servers", methods=["GET"])
def list_servers():
    cfg = ConfigManager.load()
    servers = [mask_server(s) for s in cfg.get("ftp_servers", [])]
    return jsonify({"servers": servers})


@bp.route("/api/servers", methods=["POST"])
def add_server():
    data = request.json
    if not data or not data.get("name") or not data.get("host"):
        return jsonify({"error": "名称和地址不能为空"}), 400

    cfg = ConfigManager.load()
    servers = cfg.setdefault("ftp_servers", [])

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
    start_keepalive(new_server["name"])
    return jsonify({"ok": True, "server": new_server})


@bp.route("/api/servers/<name>", methods=["DELETE"])
def delete_server(name):
    cfg = ConfigManager.load()
    servers = cfg.get("ftp_servers", [])
    before = len(servers)
    cfg["ftp_servers"] = [s for s in servers if s["name"] != name]
    if len(cfg["ftp_servers"]) == before:
        return jsonify({"error": f"未找到服务器 '{name}'"}), 404
    ConfigManager.save(cfg)
    with _ftp_status_lock:
        _ftp_status.pop(name, None)
    with _server_locks_guard:
        _server_locks.pop(name, None)
    stop_keepalive(name)
    with _keepalive_guard:
        _keepalive_threads.pop(name, None)
        _keepalive_stops.pop(name, None)
    return jsonify({"ok": True})


@bp.route("/api/servers/<name>/test", methods=["POST"])
def test_server(name):
    cfg = ConfigManager.load()
    server = None
    for s in cfg.get("ftp_servers", []):
        if s["name"] == name:
            server = s
            break
    if not server:
        return jsonify({"status": "unknown", "message": "服务器不存在"}), 404
    with _ftp_status_lock:
        _ftp_status[name] = {
            "status": "checking",
            "message": "检测中...",
            "timestamp": time.time(),
        }
    t = threading.Thread(target=_run_ftp_test_async, args=(name, server), daemon=True)
    t.start()
    return jsonify({"status": "checking", "message": "检测中..."})


@bp.route("/api/servers/<name>/status", methods=["GET"])
def server_status(name):
    with _ftp_status_lock:
        status = _ftp_status.get(name)
    if status:
        return jsonify(status)
    return jsonify({"status": "unknown", "message": "未检测"})
