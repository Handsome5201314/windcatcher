FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 python3-pip python3-venv \
    libgl1 libglib2.0-0 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

# The official apt URL redirects HTTPS downloads to HTTP. Download its exact
# release over HTTPS instead; the digest is from the official Jammy package index.
RUN curl -fSL --proto '=https' --proto-redir '=https' --retry 3 \
    https://downloads.sourceforge.net/project/foam/foam/ubuntu/dists/jammy/main/binary-amd64/openfoam11_20240612_amd64.deb -o /tmp/openfoam11.deb \
    && echo '15ffde14da1ed53b76bc5a5dee8dfed1bb707040b45fd26fff259fdb7bd63840  /tmp/openfoam11.deb' | sha256sum -c - \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/openfoam11.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/openfoam11.deb

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OPENFOAM_MPI_PROCS=2 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 \
    WINDCATCHER_MONITOR_DIR=/data/monitor \
    WINDCATCHER_AUTOMATION_DIR=/data/automation \
    WINDCATCHER_IDENTITY_DIR=/data/identity \
    WINDCATCHER_SHARE_DIR=/data/shares
WORKDIR /opt/windcatcher/app
COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt

# Explicit copies keep local cases, credentials, logs and Git history out of the image.
COPY server.py cloud.py index.html LICENSE ./
COPY pipeline/ ./pipeline/
COPY static/ ./static/
COPY deploy/ ./deploy/
COPY scripts/backup_identity.py ./scripts/backup_identity.py
RUN useradd --create-home --uid 1000 windcatcher \
    && mkdir -p /data/cases /data/monitor /data/automation /data/identity /data/shares \
    && ln -s /data/cases cases \
    && chown -R windcatcher:windcatcher /data /opt/windcatcher \
    && chmod +x deploy/start.sh
USER windcatcher
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=3)"
ENTRYPOINT ["/bin/bash", "deploy/start.sh"]
