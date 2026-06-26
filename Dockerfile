# Build stage — compiler and dev headers stay here, never reach runtime
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Use virtualenv (bundles pip, bypasses ensurepip) — python:3.11-slim on Trixie
# has no python3.11-venv apt package, so ensurepip fails with plain `python -m venv`.
# Upgrade pip first: pip 24.0 in the base image has a hash() TypeError that breaks
# dependency resolution when installing virtualenv.
RUN pip install --upgrade pip && pip install --no-cache-dir virtualenv && virtualenv /venv

# Upgrade pip and wheel to patched versions (CVE-2026-24049, CVE-2025-8869 et al.)
RUN /venv/bin/pip install --upgrade "pip==26.1" "wheel==0.46.2"

COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

# Runtime stage — clean image, no compiler, no build tools, no perl
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy the entire venv from builder — captures all transitive dependencies
COPY --from=builder /venv /venv

# 1. Let Playwright install Chromium and all its system dependencies
# 2. Add runtime shared libraries for lxml
# 3. Purge perl (no upstream fix — CVE-2026-42496 CRITICAL et al.)
# 4. Remove system pip/wheel — app uses /venv exclusively; uninstalling eliminates
#    CVE-2026-24049 / CVE-2025-8869 flags without the segfault that `pip install`
#    triggers on Trixie after playwright's --with-deps modifies the system state.
# All in one layer to minimise final image size.
RUN /venv/bin/playwright install chromium --with-deps \
    && apt-get install -y --no-install-recommends \
       libxml2 \
       libxslt1.1 \
    && apt-get purge -y --allow-remove-essential perl perl-base \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && pip uninstall -y pip wheel setuptools 2>/dev/null || true

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
