FROM docker.io/denoland/deno:bin-2.9.4 AS deno
FROM docker.io/python:3.14.6-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt update && apt install ffmpeg -y && \
    groupadd -r zimmporter -g 51000 && \
    useradd -r -g zimmporter -u 51000 -d /zimmer zimmporter

RUN mkdir -p /etc/ssl/certs
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /zimmer/

COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

COPY zimmporter/ ./zimmporter/
COPY api/ ./api/
COPY tasks/ ./tasks/
COPY db/ ./db/

RUN mkdir -p /data/zimmer/importer && chown -R 51000:51000 /zimmer /data/zimmer

ENV PYTHONUNBUFFERED=1

USER 51000

EXPOSE 8000

CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
