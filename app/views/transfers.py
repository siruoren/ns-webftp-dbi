import os
import time
import uuid
import threading
from collections import deque
from flask import Blueprint, request, jsonify

from app.models.config import ConfigManager
from app.models.transfer import (
    _transfer_tasks, _transfer_lock, FTPManager,
    cleanup_old_tasks,
)
from app.models.server_logs import cleanup_old_logs

bp = Blueprint("transfers", __name__)

_last_cleanup_ts = 0
_CLEANUP_INTERVAL = 30


@bp.route("/api/transfer", methods=["POST"])
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

    with _transfer_lock:
        task_refs = list(_transfer_tasks.values())
    existing_paths = set()
    for t in task_refs:
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
        "status": "pending",
        "create_time": time.time(),
        "total_files": len(file_list),
        "total_bytes": sum(f["size"] for f in file_list),
        "uploaded_bytes": 0,
        "current_file": "",
        "current_file_index": 0,
        "current_file_bytes": 0,
        "current_file_size": 0,
        "log": deque(maxlen=100),
        "server": server_name,
        "server_host": f"{server_info['host']}:{server_info['port']}",
        "cancelled": False,
        "start_time": None,
        "end_time": None,
        "avg_speed": 0,
        "server_info": server_info,
        "files": [
            {
                "name": f["name"],
                "path": f["path"],
                "size": f["size"],
                "status": "pending",
                "uploaded_bytes": 0,
                "progress": 0,
                "error": None,
            }
            for f in file_list
        ],
    }
    with _transfer_lock:
        _transfer_tasks[task_id] = task

    result = {"task_id": task_id, "total_files": len(file_list),
              "total_bytes": task["total_bytes"]}
    if skipped:
        result["skipped"] = len(skipped)
    return jsonify(result)


@bp.route("/api/transfers/start-all", methods=["POST"])
def start_all_pending_transfers():
    data = request.json or {}
    server_name = data.get("server")
    if not server_name:
        return jsonify({"error": "缺少服务器名称"}), 400

    started = []
    with _transfer_lock:
        for tid, task in _transfer_tasks.items():
            if task.get("server") != server_name:
                continue
            if task.get("status") != "pending":
                continue
            server_info = task.get("server_info")
            if not server_info:
                continue
            task["status"] = "starting"
            started.append((tid, server_info, task.get("create_time") or 0))

    started.sort(key=lambda x: x[2])

    def _sequential_upload():
        for tid, sinfo, _ in started:
            FTPManager.upload_files(tid, sinfo)

    thread = threading.Thread(target=_sequential_upload, daemon=True)
    thread.start()

    return jsonify({"ok": True, "started": len(started)})


@bp.route("/api/transfer/<task_id>/cancel", methods=["POST"])
def cancel_transfer(task_id):
    with _transfer_lock:
        task = _transfer_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    task["cancelled"] = True
    if not task.get("end_time"):
        task["end_time"] = time.time()
        task["status"] = "cancelled"
    return jsonify({"ok": True})


@bp.route("/api/transfer/<task_id>", methods=["DELETE"])
def delete_transfer(task_id):
    with _transfer_lock:
        if task_id in _transfer_tasks:
            del _transfer_tasks[task_id]
            return jsonify({"ok": True})
    return jsonify({"error": "任务不存在"}), 404


@bp.route("/api/transfers")
def list_transfers():
    global _last_cleanup_ts
    now = time.time()
    if now - _last_cleanup_ts >= _CLEANUP_INTERVAL:
        cleanup_old_tasks()
        cleanup_old_logs()
        _last_cleanup_ts = now
    with _transfer_lock:
        task_refs = list(_transfer_tasks.values())
    tasks = []
    for task in task_refs:
        tasks.append({
            "id": task["id"],
            "status": task["status"],
            "server": task.get("server", ""),
            "server_host": task.get("server_host", ""),
            "total_files": task["total_files"],
            "total_bytes": task["total_bytes"],
            "uploaded_bytes": task["uploaded_bytes"],
            "current_file": task["current_file"],
            "current_file_index": task["current_file_index"],
            "create_time": task.get("create_time"),
            "start_time": task.get("start_time"),
            "end_time": task.get("end_time"),
            "files": task.get("files", []),
        })
    active_statuses = {"starting", "transferring"}
    pending_statuses = {"pending"}
    tasks.sort(key=lambda t: (
        0 if t["status"] in active_statuses else (1 if t["status"] in pending_statuses else 2),
        t.get("create_time") or 0
    ))
    return jsonify({"transfers": tasks})


@bp.route("/api/transfer/<task_id>/cancel-files", methods=["POST"])
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


@bp.route("/api/transfer/<task_id>/retry-files", methods=["POST"])
def retry_files(task_id):
    data = request.json or {}
    file_indices = data.get("file_indices", [])
    with _transfer_lock:
        task = _transfer_tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task["status"] not in ("completed", "error", "cancelled"):
        return jsonify({"error": "任务仍在运行中，无法重试"}), 400

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

    server_info = task.get("server_info")
    if not server_info:
        cfg = ConfigManager.load()
        for s in cfg.get("ftp_servers", []):
            if s["name"] == task.get("server"):
                server_info = s
                break
    if not server_info:
        return jsonify({"error": "服务器配置不存在"}), 400
    task["server_info"] = server_info

    task["status"] = "starting"
    task["cancelled"] = False
    task["start_time"] = None
    task["end_time"] = None
    task["error"] = None
    task["current_file"] = ""
    task["current_file_index"] = 0
    task["current_file_bytes"] = 0
    task["current_file_size"] = 0

    thread = threading.Thread(
        target=FTPManager.upload_files,
        args=(task_id, server_info),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "retried": reset_count})
