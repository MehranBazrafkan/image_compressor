from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from datetime import date, datetime, timedelta
from PIL import Image, UnidentifiedImageError
from typing import Dict, Tuple
from io import BytesIO
import uuid
import os

# -----------------------
# Configuration
# -----------------------
ALLOWED_OUTPUT_FORMATS = {"JPEG", "PNG", "WEBP"}
DEFAULT_QUALITY = 40
MAX_QUALITY = 100
MIN_QUALITY = 1

# limit uploads to 10 MB (adjust if needed)
MAX_CONTENT_LENGTH = 10 * 1024 * 1024

# how long to keep compressed image bytes in memory (seconds)
DOWNLOAD_CACHE_TTL_SECONDS = 300  # 5 minutes

# optional debug and port
DEBUG = True
DEFAULT_PORT = 5000

# -----------------------
# App setup
# -----------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# In-memory download cache:
# mapping download_id -> (bytes, mimetype, filename, expiry_datetime)
DownloadEntry = Tuple[bytes, str, str, datetime]
app.download_cache: Dict[str, DownloadEntry] = {}

# -----------------------
# Helpers
# -----------------------
def _cleanup_expired_cache() -> None:
    """Remove expired entries from app.download_cache. Called during requests."""
    now = datetime.utcnow()
    expired = [k for k, (_, _, _, expiry) in app.download_cache.items() if expiry <= now]
    for k in expired:
        app.download_cache.pop(k, None)


def _generate_download_id() -> str:
    return uuid.uuid4().hex


def _safe_output_extension(fmt: str) -> str:
    """Return common file extension for the chosen output format."""
    fmt = fmt.upper()
    if fmt == "JPEG":
        return "jpg"
    if fmt == "PNG":
        return "png"
    if fmt == "WEBP":
        return "webp"
    # fallback
    return fmt.lower()


# -----------------------
# Routes
# -----------------------
@app.route("/", methods=["GET"])
def index():
    """Render main page."""
    # cleanup on each page view to avoid memory build-up
    _cleanup_expired_cache()
    return render_template(
        "index.html",
        current_year=date.today().year,
        result=None,
        allowed_formats=sorted(ALLOWED_OUTPUT_FORMATS),
    )


@app.route("/compress", methods=["POST"])
def compress_image():
    """Handle uploaded image, compress/convert and put result in in-memory cache.

    Returns the same index page with `result` containing stats + download id.
    """
    # small cleanup call
    _cleanup_expired_cache()

    if "file" not in request.files:
        flash("No file part in request.", "error")
        return redirect(url_for("index"))

    file = request.files["file"]
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("index"))

    # get and validate user options
    out_format = (request.form.get("format") or "JPEG").upper()
    if out_format not in ALLOWED_OUTPUT_FORMATS:
        flash("Unsupported output format selected.", "error")
        return redirect(url_for("index"))

    try:
        quality = int(request.form.get("quality", DEFAULT_QUALITY))
    except (TypeError, ValueError):
        quality = DEFAULT_QUALITY

    # clamp quality to safe range
    quality = max(MIN_QUALITY, min(MAX_QUALITY, quality))

    # compute original file size (in KB) safely from the stream
    try:
        # ensure stream at start and measure bytes
        file.stream.seek(0, os.SEEK_END)
        orig_bytes = file.stream.tell()
        file.stream.seek(0)
        orig_size_kb = orig_bytes / 1024.0
    except Exception:
        # fallback (should rarely happen)
        orig_size_kb = 0.0

    # open and validate with Pillow
    try:
        img = Image.open(file.stream)
        img.verify()  # ensure file is an actual image (raises if not)
    except UnidentifiedImageError:
        flash("Uploaded file is not a valid image.", "error")
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"Error validating image: {e}", "error")
        return redirect(url_for("index"))

    # reopen because verify() leaves the file in an unusable state
    file.stream.seek(0)
    try:
        img = Image.open(file.stream)
    except Exception as e:
        flash(f"Cannot open image after verification: {e}", "error")
        return redirect(url_for("index"))

    # convert if necessary (e.g. PNG alpha -> RGB for JPEG/WebP)
    if img.mode in ("RGBA", "P") and out_format != "PNG":
        img = img.convert("RGB")

    # prepare BytesIO and save compressed image
    img_io = BytesIO()
    save_kwargs = {"format": out_format}
    # JPEG/WEBP accept quality; PNG uses 'optimize' / compress_level if needed
    if out_format in ("JPEG", "WEBP"):
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    elif out_format == "PNG":
        # Pillow uses compress_level (0-9). Map quality 1-100 -> 9-0 (higher quality => lower compression)
        # We'll compute a simple mapping: compress_level = round((100 - quality) / 11.111...) clamp 0-9
        compress_level = max(0, min(9, round((100 - quality) / (100 / 9))))
        save_kwargs["optimize"] = True
        save_kwargs["compress_level"] = compress_level

    try:
        img.save(img_io, **save_kwargs)
    except Exception as e:
        flash(f"Error saving compressed image: {e}", "error")
        return redirect(url_for("index"))

    img_io.seek(0)
    compressed_bytes = img_io.getvalue()
    compressed_size_kb = len(compressed_bytes) / 1024.0
    # avoid division by zero
    if orig_size_kb > 0:
        compression_percent = 100.0 * (orig_size_kb - compressed_size_kb) / orig_size_kb
    else:
        compression_percent = 0.0

    # store bytes in in-memory cache with TTL
    download_id = _generate_download_id()
    ext = _safe_output_extension(out_format)
    filename = f"compressed.{ext}"
    mimetype = f"image/{ext if ext != 'jpg' else 'jpeg'}"  # standardize MIME
    expiry = datetime.utcnow() + timedelta(seconds=DOWNLOAD_CACHE_TTL_SECONDS)
    app.download_cache[download_id] = (compressed_bytes, mimetype, filename, expiry)

    # prepare result dict for rendering
    result = {
        "original_kb": round(orig_size_kb, 2),
        "compressed_kb": round(compressed_size_kb, 2),
        "percent": round(compression_percent, 2),
        "format": out_format,
        "quality": quality,
        "download_id": download_id,
        "filename": filename,
    }

    return render_template(
        "index.html",
        current_year=date.today().year,
        result=result,
        allowed_formats=sorted(ALLOWED_OUTPUT_FORMATS),
    )


@app.route("/download/<download_id>", methods=["GET"])
def download_image(download_id: str):
    """Serve the compressed image bytes and remove them from cache afterwards.

    This prevents files lingering indefinitely in memory. Also verifies expiry.
    """
    _cleanup_expired_cache()

    entry = app.download_cache.pop(download_id, None)
    if entry is None:
        flash("Download is no longer available or has expired.", "error")
        return redirect(url_for("index"))

    compressed_bytes, mimetype, filename, expiry = entry
    # expiry already enforced in cleanup; extra check
    if expiry <= datetime.utcnow():
        flash("Download has expired.", "error")
        return redirect(url_for("index"))

    # serve bytes as file-like stream
    return send_file(
        BytesIO(compressed_bytes),
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename,
    )


# -----------------------
# Error handlers
# -----------------------
@app.errorhandler(413)
def request_entity_too_large(error):
    flash("Uploaded file is too large. Max allowed size: {} MB.".format(MAX_CONTENT_LENGTH // (1024 * 1024)), "error")
    return redirect(url_for("index"))


# -----------------------
# CLI / run
# -----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    app.run(host="0.0.0.0", port=port, debug=DEBUG)
