# syntax=docker/dockerfile:1
# Self-contained: yt-dlp, ffmpeg and a JS runtime (deno) are all bundled,
# so the host only needs Docker. Multi-arch (amd64/arm64).
ARG DENO_TAG=bin
FROM denoland/deno:${DENO_TAG} AS deno

# Static ffmpeg from yt-dlp's own builds (patched for yt-dlp). Only ffmpeg is
# kept: merging and mp3 extraction work without ffprobe.
FROM python:3.13-slim AS ffmpeg
ARG TARGETARCH
RUN python - <<'EOF'
import os, platform, tarfile, urllib.request
arch = os.environ.get("TARGETARCH") or {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
name = {"amd64": "linux64", "arm64": "linuxarm64"}[arch]
url = f"https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-{name}-gpl.tar.xz"
path, _ = urllib.request.urlretrieve(url)
with tarfile.open(path) as tar:
    m = next(m for m in tar.getmembers() if m.name.endswith("/bin/ffmpeg"))
    m.name = "ffmpeg"
    tar.extract(m, "/out", filter="data")
EOF

FROM python:3.13-slim

ARG YTDLP_VERSION=2026.08.19

COPY --from=ffmpeg /out/ffmpeg /usr/local/bin/ffmpeg
# yt-dlp needs an external JS runtime for full YouTube support.
COPY --from=deno /deno /usr/local/bin/deno

RUN pip install --no-cache-dir "yt-dlp[default]==${YTDLP_VERSION}"

RUN useradd --create-home --uid 1000 tubby \
 && mkdir -p /data && chown tubby:tubby /data

WORKDIR /app
COPY server.py ./
COPY static ./static

# Fail the build, not the first download, if anything bundled is broken.
RUN python server.py --check

USER tubby
ENV TUBBY_DIR=/data \
    TUBBY_YTDLP=yt-dlp \
    PYTHONUNBUFFERED=1
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"TUBBY_PORT\",\"8080\")}/healthz', timeout=4)"

STOPSIGNAL SIGTERM
CMD ["python", "server.py"]
