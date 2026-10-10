ARG PYTHON_IMAGE=python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
FROM ${PYTHON_IMAGE} AS tools

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
# Pin the system library used by this Python base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends liblzma5=5.8.1-1+deb13u2 \
    && rm -rf /var/lib/apt/lists/*
COPY bootstrap.txt requirements.txt ./
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r bootstrap.txt \
    && python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt \
    && python -m pip check \
    && groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --no-create-home gateway
COPY --chown=gateway:gateway __init__.py app.py contracts.py middleware.py ./gateway/

FROM tools AS runtime
RUN python -m pip uninstall --yes pip
USER gateway
EXPOSE 8080
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--limit-concurrency", "80", "--no-server-header"]
