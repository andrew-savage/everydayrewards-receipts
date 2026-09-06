FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EDR_DATA_DIR=/data \
    EDR_OUTPUT_DIR=/receipts

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
    && useradd --uid 1000 --create-home app \
    && mkdir -p /data /receipts \
    && chown app:app /data /receipts

USER app
VOLUME ["/data", "/receipts"]

HEALTHCHECK --interval=5m --timeout=15s --start-period=2m --retries=3 \
    CMD ["everyday-receipts", "healthcheck"]

ENTRYPOINT ["everyday-receipts"]
CMD ["run"]
