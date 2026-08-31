from flask import Blueprint, jsonify

from app.models.keepalive import (
    start_keepalive, stop_keepalive, _get_server_info,
    _keepalive_threads, _keepalive_guard,
)

bp = Blueprint("keepalive", __name__)


@bp.route("/api/keepalive/<server>", methods=["GET"])
def keepalive_status(server):
    with _keepalive_guard:
        thread = _keepalive_threads.get(server)
        is_alive = bool(thread and thread.is_alive())
    return jsonify({"server": server, "keepalive": is_alive})


@bp.route("/api/keepalive/<server>", methods=["POST"])
def keepalive_start(server):
    if not _get_server_info(server):
        return jsonify({"ok": False, "error": "实例不存在"}), 404
    start_keepalive(server)
    return jsonify({"ok": True, "server": server})


@bp.route("/api/keepalive/<server>", methods=["DELETE"])
def keepalive_stop(server):
    stop_keepalive(server)
    return jsonify({"ok": True, "server": server})
