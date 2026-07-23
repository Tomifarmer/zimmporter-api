FROM docker.io/denoland/deno:bin-2.9.4 AS deno
FROM docker.io/python:3.14.6-slim-trixie

RUN apt update && apt install ffmpeg -y
RUN mkdir -p /etc/ssl/certs
COPY --from=deno /deno /usr/local/bin/deno

COPY requirements.txt /zimmer/
COPY zimmporter/ /zimmer/zimmporter/
COPY api/ /zimmer/api/
COPY tasks/ /zimmer/tasks/
COPY db/ /zimmer/db/

WORKDIR /zimmer/

RUN pip install -r requirements.txt

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
