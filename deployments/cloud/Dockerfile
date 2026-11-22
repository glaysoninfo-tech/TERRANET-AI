FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TERRANET_CORE_HOST=0.0.0.0
ENV TERRANET_TRACK_HOST=0.0.0.0

COPY pyproject.toml README.md /app/
COPY common /app/common
COPY contracts /app/contracts
COPY edge /app/edge
COPY core /app/core

RUN pip install --no-cache-dir ".[core]" && \
    groupadd -g 10001 terranet && \
    useradd -u 10001 -g 10001 -m terranet && \
    mkdir -p /data && chown 10001:10001 /data

USER 10001:10001

EXPOSE 8000

CMD ["terranet-core"]
