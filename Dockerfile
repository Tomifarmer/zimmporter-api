FROM docker.io/python:3.15.2-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

RUN apt update && apt upgrade --no-install-recommends -y && \
    apt install --no-install-recommends ca-certificates libexpat1 -y && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -r zimmporter -g 51000 && \
    useradd -r -g zimmporter -u 51000 -d /zimmer zimmporter && \
    mkdir -p /etc/ssl/certs /data/zimmer/importer /var/zimmporter/cookies

WORKDIR /zimmer/

# Remove pip after installing dependencies: the runtime does not need it, and
# pip's vendored libraries (msgpack/setuptools/urllib3) trigger Trivy findings.
COPY requirements.txt requirements-base.txt ./
RUN uv pip install --system --no-cache -r requirements.txt && \
    rm -rf /usr/local/lib/python3.15/site-packages/pip /usr/local/lib/python3.15/site-packages/pip-*.dist-info

COPY zimmporter/ ./zimmporter/
COPY api/ ./api/
COPY tasks/ ./tasks/
COPY db/ ./db/

RUN chown -R 51000:51000 /zimmer /data/zimmer /var/zimmporter

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

USER 51000

EXPOSE 8000

CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
