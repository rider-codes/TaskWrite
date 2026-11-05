FROM python:3.12-slim

# Run as an ordinary user, not root: generated code and uploaded files never get root rights.
RUN useradd --create-home --uid 10001 taskwrite \
    && mkdir -p /data/workspaces && chown -R taskwrite /data
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY site ./site

ENV DATABASE_URL=sqlite:////data/taskwrite.db \
    WORKSPACE_DIR=/data/workspaces \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER taskwrite
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/sandbox', timeout=4)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
