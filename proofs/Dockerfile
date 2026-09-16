# PiperStitch Proofs as a container (Railway builds this when the service's
# root directory is proofs/). Same shape as License Admin's: plain uvicorn,
# SQLite and artifacts on a volume mounted at /data, root on purpose
# (Railway mounts volumes root-owned).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/proofs.db ARTIFACT_DIR=/data/artifacts
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
RUN mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "mkdir -p \"$(dirname \"$DATABASE_PATH\")\" \"$ARTIFACT_DIR\" && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
