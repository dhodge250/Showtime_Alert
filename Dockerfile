FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install OS-level deps needed by lxml and requests
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer cache — only rebuilds if requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create the data directory that will be bind-mounted for SQLite persistence
RUN mkdir -p /app/data

# Expose Flask port
EXPOSE 5000

# Disable Python output buffering so logs appear immediately in docker logs
ENV PYTHONUNBUFFERED=1

CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "wsgi:app"]
