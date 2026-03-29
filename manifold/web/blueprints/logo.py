"""Logo blueprint — serve and upload channel logos."""

import re

from flask import Blueprint, send_file, abort, request, jsonify

from manifold.services.logo_manager import LogoManagerService

logo_bp = Blueprint("logo", __name__)

MANIFEST_ID_RE = re.compile(r"^[a-f0-9-]+$")


@logo_bp.route("/<manifest_id>")
def serve_logo(manifest_id):
    if not MANIFEST_ID_RE.match(manifest_id):
        abort(400)
    path = LogoManagerService.get_logo_path(manifest_id)
    if not path:
        abort(404)
    return send_file(path, mimetype="image/png")


@logo_bp.route("/<manifest_id>", methods=["POST"])
def upload_logo(manifest_id):
    if not MANIFEST_ID_RE.match(manifest_id):
        abort(400)
    if request.content_type and "multipart" in request.content_type:
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "no file"}), 400
        data = f.read()
    else:
        data = request.get_data()
    if not data or len(data) < 100:
        return jsonify({"error": "empty or too small"}), 400
    ok = LogoManagerService.save_logo(manifest_id, data)
    if not ok:
        return jsonify({"error": "save failed"}), 500
    # Mark logo_cached on the manifest
    from manifold.database import get_session
    from manifold.models.manifest import Manifest
    with get_session() as session:
        m = session.query(Manifest).filter_by(id=manifest_id).first()
        if m:
            m.logo_cached = True
    return jsonify({"ok": True})
