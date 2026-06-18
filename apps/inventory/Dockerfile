# Build-Kontext = Repository-Root (siehe sites/dc/docker-compose.yml: context: ../..).
# Ein gemeinsames Image fuer Setup (bootstrap/prepare/migrate via ops.db) UND Runtime
# (FastAPI). Nur Runtime-Requirements, KEINE Dev-Abhaengigkeiten.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Runtime-Requirements zuerst (Layer-Caching).
COPY apps/inventory/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Anwendungscode, Migrationen und geteilte Ops-Tools.
COPY apps/inventory/app ./app
COPY apps/inventory/migrations ./migrations
COPY ops ./ops

# Nicht privilegierter Runtime-User.
RUN useradd --system --uid 10001 appuser
USER appuser

EXPOSE 8000
# Runtime-Default; die Setup-Services ueberschreiben command (ops.db.*).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
