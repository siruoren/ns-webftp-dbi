import time
from collections import deque
from datetime import datetime
from flask import Blueprint, request, jsonify

from app.models.server_logs import _server_logs, _server_logs_lock, _MAX_LOG_ENTRIES
from app.models.transfer import LOG_RETENTION_SECONDS

bp = Blueprint("logs", __name__)


@bp.route("/api/logs/<server>", methods=["POST"])
def add_server_log(server):
    data = request.json or {}
    msg = (data.get("msg") or "").strip()
    level = data.get("level") or "info"
    ts = data.get("ts") or time.time()
    if not msg:
        return jsonify({"ok": False, "error": "msg is required"}), 400
    entry = {"ts": float(ts), "msg": msg, "level": level}
    with _server_logs_lock:
        if server not in _server_logs:
            _server_logs[server] = deque(maxlen=_MAX_LOG_ENTRIES)
        _server_logs[server].append(entry)
    return jsonify({"ok": True})


@bp.route("/api/logs/<server>", methods=["GET"])
def get_server_log(server):
    now = time.time()
    with _server_logs_lock:
        entries = list(_server_logs.get(server, []))
    entries = [e for e in entries if now - e["ts"] <= LOG_RETENTION_SECONDS]
    entries.reverse()
    result = []
    for e in entries:
        result.append({
            "time": datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S"),
            "msg": e["msg"],
            "level": e["level"],
        })
    return jsonify({"logs": result})


@bp.route("/api/logs/<server>", methods=["DELETE"])
def clear_server_log(server):
    with _server_logs_lock:
        if server in _server_logs:
            del _server_logs[server]
    return jsonify({"ok": True})
