FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/yarn.lock ./
RUN yarn install --frozen-lockfile
COPY frontend/ ./
RUN yarn build

FROM python:3.12-alpine
RUN adduser -D -u 10001 mailbox && mkdir /data && chown mailbox:mailbox /data
WORKDIR /app
COPY backend/*.py backend/
COPY --from=frontend /build/dist frontend/dist
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    STATIC_DIR=/app/frontend/dist \
    SMTP_PORT=2525 \
    HTTP_PORT=8080
USER mailbox
VOLUME /data
EXPOSE 2525 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=3)"
CMD ["python", "backend/server.py"]
