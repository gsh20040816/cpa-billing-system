FROM node:22-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib
ENV BILLING_FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
COPY pyproject.toml README.md ./
COPY cpa_billing ./cpa_billing
COPY static ./static
COPY alembic.ini ./
COPY migrations ./migrations
COPY --from=frontend /frontend/dist ./frontend/dist
RUN pip install --no-cache-dir .
ENTRYPOINT ["cpa-billing"]
