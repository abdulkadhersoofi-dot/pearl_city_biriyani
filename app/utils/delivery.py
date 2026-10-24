"""Getting pages to phones quickly (the ARFA Counter app on a mobile
connection, and every browser).

Static files are fingerprinted: url_for('static', ...) adds ?v=<a short
hash of the file>, and a fingerprinted file is cached by the browser (and
the app's service worker) for a year without asking again - a changed file
gets a new hash, so a new copy is fetched exactly when it changes. Before
this every page re-asked the server about every script and stylesheet.

Text responses (pages, CSS, scripts, JSON, SVG) are gzip-compressed for a
client that accepts it - about a quarter of the size on the wire.
Compressed static files are kept in memory, so each is compressed once."""

import gzip
import hashlib
import os

from flask import request

YEAR = 365 * 24 * 3600
MIN_BYTES = 1024
COMPRESSIBLE = ("text/html", "text/css", "text/plain", "application/javascript", "text/javascript",
                "application/json", "image/svg+xml", "application/manifest+json")

_hashes: dict[str, tuple[float, str]] = {}
_gzipped: dict[tuple[str, str], bytes] = {}


def static_version(app, filename: str) -> str | None:
    path = os.path.join(app.static_folder, filename)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    known = _hashes.get(path)
    if known and known[0] == mtime:
        return known[1]
    with open(path, "rb") as fh:
        digest = hashlib.sha1(fh.read()).hexdigest()[:10]
    _hashes[path] = (mtime, digest)
    return digest


def init_delivery(app) -> None:
    @app.url_defaults
    def _fingerprint_static(endpoint, values):
        if endpoint == "main.media" and "image_id" in values and "sig" not in values:
            from app.main.routes import media_sig

            values["sig"] = media_sig(values["image_id"])
            return
        if endpoint == "static" and "filename" in values and "v" not in values:
            version = static_version(app, values["filename"])
            if version:
                values["v"] = version

    @app.after_request
    def _cache_and_compress(response):
        if request.endpoint == "static" and response.status_code == 200 and request.args.get("v"):
            response.cache_control.no_cache = None
            response.cache_control.public = True
            response.cache_control.max_age = YEAR
            response.cache_control.immutable = True
        return _compress(response)


def _compress(response):
    if (response.status_code != 200 or response.headers.get("Content-Encoding")
            or "gzip" not in (request.headers.get("Accept-Encoding") or "")
            or (response.mimetype or "") not in COMPRESSIBLE or response.is_streamed and not response.direct_passthrough):
        return response
    key = None
    if request.endpoint == "static" and request.args.get("v"):
        key = (request.path, request.args["v"])
    if key and key in _gzipped:
        body = _gzipped[key]
    else:
        response.direct_passthrough = False
        data = response.get_data()
        if len(data) < MIN_BYTES:
            return response
        body = gzip.compress(data, compresslevel=6)
        if key:
            _gzipped[key] = body
    response.direct_passthrough = False
    response.set_data(body)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(body))
    response.vary.add("Accept-Encoding")
    # The uncompressed file's validator doesn't describe these bytes.
    if response.headers.get("ETag"):
        etag, weak = response.get_etag()
        if etag:
            response.set_etag(etag + "-gz", weak=True)
    return response
