# Build stage — compiler and dev headers stay here, never reach runtime
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a venv so all packages (including transitive deps) are self-contained
RUN python -m venv /venv

# Upgrade pip and wheel to patched versions (CVE-2026-24049, CVE-2025-8869 et al.)
RUN /venv/bin/pip install --upgrade "pip==26.1" "wheel==0.46.2"

COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

# Runtime stage — clean image, no compiler, no build tools, no perl
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install runtime shared libraries for lxml; purge perl (no upstream fix —
# CVE-2026-42496 CRITICAL, CVE-2026-9538/48959/42497/48962 HIGH);
# upgrade system pip/wheel to patched versions (CVE-2026-24049, CVE-2025-8869 et al.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    && apt-get purge -y --allow-remove-essential perl perl-base \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --upgrade "pip>=26.1" "wheel>=0.46.2"

# Copy the entire venv from builder — captures all transitive dependencies
COPY --from=builder /venv /venv

COPY . .

RUN mkdir -p /app/data

EXPOSE 5000

ENV PYTHONUNBUFFERED=1

CMD ["/venv/bin/gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "wsgi:app"]
