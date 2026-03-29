"""Output blueprint — serve generated M3U and XMLTV files."""

import os

from flask import Blueprint, send_file, abort

from manifold.config import Config

output_bp = Blueprint("output", __name__)


@output_bp.route("/manifold.m3u")
def serve_m3u():
    path = os.path.join(Config.OUTPUT_DIR, "manifold.m3u")
    if not os.path.isfile(path):
        abort(404, "M3U not yet generated")
    return send_file(path, mimetype="application/octet-stream",
                     as_attachment=True, download_name="manifold.m3u")


@output_bp.route("/manifold.xml")
def serve_xmltv():
    path = os.path.join(Config.OUTPUT_DIR, "manifold.xml")
    if not os.path.isfile(path):
        abort(404, "XMLTV not yet generated")
    return send_file(path, mimetype="application/octet-stream",
                     as_attachment=True, download_name="manifold.xml")


@output_bp.route("/program-image/<filename>")
def serve_program_image(filename):
    program_image_dir = os.path.join(Config.DATA_DIR, "program_images")
    path = os.path.join(program_image_dir, filename)
    if not os.path.isfile(path):
        abort(404, "Image not found")
    response = send_file(path, mimetype="image/jpeg")
    response.cache_control.max_age = 86400  # 1 day
    response.cache_control.public = True
    return response
