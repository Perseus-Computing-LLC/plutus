# Plutus — the billing layer for AI agents.
# One-command self-hosted deploy:  docker run -p 8420:8420 plutus-agent
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Plutus"
LABEL org.opencontainers.image.description="The billing layer for AI agents — self-hosted usage metering + prepaid-credit billing."
LABEL org.opencontainers.image.source="https://github.com/Perseus-Computing-LLC/plutus"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PLUTUS_HOME=/data \
    PLUTUS_PORT=8420

WORKDIR /app

# Install the package (with Stripe + PDF extras) from source.
COPY pyproject.toml README.md LICENSE ./
COPY plutus_agent ./plutus_agent
RUN pip install --no-cache-dir ".[all]"

# State (config + SQLite) persists in a mounted volume.
VOLUME ["/data"]
EXPOSE 8420

# Bind to all interfaces inside the container; map the port on the host.
# Default to the demo so `docker run -p 8420:8420 plutus-agent` shows value
# instantly. For real use, override the command:
#   docker run -p 8420:8420 -v plutus:/data plutus-agent serve --host 0.0.0.0
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["plutus"]
CMD ["serve", "--demo", "--host", "0.0.0.0"]
