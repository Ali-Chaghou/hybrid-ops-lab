# Eigenes, minimales Publisher-Image (Build-Kontext = apps/publisher/). Es wird NUR
# der Publisher-App-Code kopiert — kein Repo-Root, keine .env, keine Terraform-/SSH-
# Dateien, keine Consumer-/ops-Verzeichnisse. Migrationen laufen NICHT hier (separate
# Admin-Rolle), daher kein ops/.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Nicht privilegierter Runtime-User.
RUN useradd --system --uid 10001 appuser
USER appuser

EXPOSE 8000
# Standardmaessig DEAKTIVIERT (PUBLISHER_ENABLED kommt aus dem Environment, Default false).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
