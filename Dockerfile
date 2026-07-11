FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
COPY pyproject.toml README.md ./
COPY cpa_billing ./cpa_billing
COPY templates ./templates
COPY static ./static
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir .
ENTRYPOINT ["cpa-billing"]
