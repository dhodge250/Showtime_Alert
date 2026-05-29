# Build stage — compiler and dev headers stay here, never reach runtime
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and wheel to patched versions (CVE-2026-24049, CVE-2025-8869 et al.)
RUN pip install --upgrade "pip==26.1" "wheel==0.46.2"

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime stage — clean image, no compiler, no build tools, no perl
FROM python:3.11-slim AS runtime

WORKDIR /app

# Only the shared libraries lxml needs at runtime (not the -dev headers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled Python packages from builder
COPY --from=builder /install /usr/local

COPY . .

RUN mkdir -p /app/data

EXPOSE 5000

ENV PYTHONUNBUFFERED=1

CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "wsgi:app"]
