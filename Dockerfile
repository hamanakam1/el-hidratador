# El Hidratador — runtime image.
#
# The package is standard library only, so the image stays small on purpose.
# The ledger is mounted, never baked in.

FROM python:3.14-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY el_hidratador ./el_hidratador
RUN python -m pip install --no-cache-dir --upgrade pip build \
    && python -m build --wheel --outdir /dist

FROM python:3.14-slim

LABEL org.opencontainers.image.title="el-hidratador" \
      org.opencontainers.image.description="Context expansion and execution staging engine" \
      org.opencontainers.image.source="https://github.com/hamanakam1/el-hidratador" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    HIDRATADOR_DB=/var/lib/hidratador/ledger.sqlite

COPY --from=builder /dist/*.whl /tmp/

RUN pip install --no-cache-dir /tmp/*.whl \
    && rm -f /tmp/*.whl \
    && useradd --create-home --uid 10001 hidratador \
    && mkdir -p /var/lib/hidratador \
    && chown -R hidratador:hidratador /var/lib/hidratador

USER hidratador
WORKDIR /home/hidratador
VOLUME ["/var/lib/hidratador"]

ENTRYPOINT ["hidratador"]
CMD ["--help"]
