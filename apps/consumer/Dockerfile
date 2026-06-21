# Build-Kontext = Repository-Root (siehe sites/cloud/docker-compose.yml und
# ops/deploy/deploy-consumer.sh: docker build -f apps/consumer/Dockerfile <root>).
# Ein gemeinsames Image fuer Setup (bootstrap/prepare/migrate via ops.db) UND
# Runtime (FastAPI-Consumer). Nur Runtime-Requirements, keine Dev-Abhaengigkeiten.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Requirements zuerst (Layer-Caching).
COPY apps/consumer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Anwendungscode, Consumer-Migrationen und NUR die fuer das DB-Setup benoetigten
# ops.db-Module (Bootstrap/Prepare/Migrate). Deploy-/Chaos-/Bootstrap-Shellskripte
# und andere ops-Teile gehoeren nicht ins Runtime-Image.
COPY apps/consumer/app ./app
COPY apps/consumer/migrations ./migrations
COPY ops/__init__.py ./ops/__init__.py
COPY ops/db ./ops/db

RUN useradd --system --uid 10001 appuser
USER appuser
EXPOSE 8000
# Runtime-Default; die Setup-Services ueberschreiben command (ops.db.*).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
