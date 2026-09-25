# cuDNN 9 is in the image on purpose: CTranslate2 (under faster-whisper) links
# against it, and the plain CUDA runtime image fails at model load with an
# error that names no library at all.
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/config/cache/huggingface \
    CLEANARR_CONFIG=/config \
    CLEANARR_CACHE=/config/cache \
    CLEANARR_WEB=/app/web

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt /app/requirements.txt
# Ubuntu 22.04's pip (22.0) crashes in its resolver - AssertionError in
# get_topological_weights - on current releases of these packages' dependencies.
RUN pip3 install --no-cache-dir --upgrade "pip>=24" \
    && pip3 install --no-cache-dir -r /app/requirements.txt

COPY server/cleanarr /app/cleanarr
COPY web /app/web

EXPOSE 8477
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "uvicorn", "cleanarr.main:app", "--host", "0.0.0.0", "--port", "8477"]
