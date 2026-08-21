import os
import secrets
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from stream_manager import StreamManager

DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOAD_DIR = Path(DATA_DIR) / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
manager = StreamManager(DATA_DIR)

ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def require_auth(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not ADMIN_USER or not ADMIN_PASSWORD:
            return fn(*args, **kwargs)
        auth = request.authorization
        ok = (
            auth
            and secrets.compare_digest(auth.username or "", ADMIN_USER)
            and secrets.compare_digest(auth.password or "", ADMIN_PASSWORD)
        )
        if not ok:
            return "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Stream247"'}
        return fn(*args, **kwargs)
    return wrapped


@app.after_request
def no_cache_api(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
@require_auth
def index():
    return render_template("index.html")


@app.route("/api/streams", methods=["GET", "POST"])
@require_auth
def streams():
    try:
        if request.method == "GET":
            return jsonify(manager.list_streams())
        return jsonify(manager.create_stream(request.get_json(silent=True) or {})), 201
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/streams/<stream_id>", methods=["GET", "PUT", "DELETE"])
@require_auth
def stream_item(stream_id):
    try:
        if request.method == "GET":
            item = manager.get_stream(stream_id)
            if not item:
                return jsonify({"error": "Stream not found"}), 404
            return jsonify(item)
        if request.method == "PUT":
            return jsonify(manager.update_stream(stream_id, request.get_json(silent=True) or {}))
        manager.delete_stream(stream_id)
        return jsonify({"ok": True})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/streams/<stream_id>/start", methods=["POST"])
@require_auth
def start_stream(stream_id):
    try:
        return jsonify(manager.start_stream(stream_id))
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/streams/<stream_id>/stop", methods=["POST"])
@require_auth
def stop_stream(stream_id):
    return jsonify(manager.stop_stream(stream_id))


@app.route("/api/streams/<stream_id>/logs")
@require_auth
def stream_logs(stream_id):
    return jsonify({"lines": manager.get_logs(stream_id)})


@app.route("/api/system/stats")
@require_auth
def system_stats():
    return jsonify(manager.get_system_stats())


@app.route("/api/probe-source", methods=["POST"])
@require_auth
def probe_source():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(manager.probe_source(payload.get("source", "")))
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/upload-logo", methods=["POST"])
@require_auth
def upload_logo():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file selected"}), 400
    ext = Path(secure_filename(file.filename)).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        return jsonify({"error": "Allowed: PNG, JPG, JPEG, WEBP"}), 400
    filename = f"logo_{secrets.token_hex(6)}{ext}"
    path = UPLOAD_DIR / filename
    file.save(path)
    return jsonify({"path": str(path), "url": f"/uploads/{filename}"})


@app.route("/uploads/<path:filename>")
@require_auth
def uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "File too large"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
