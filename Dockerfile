# TENTACLE on Railway (or any container host). The loop runs 24/5; the Claude
# decision-maker uses the Anthropic API (set BACKEND=api + ANTHROPIC_API_KEY), since
# the subscription `claude` CLI cannot authenticate headlessly in a container.
#
# Secrets are NOT baked in (see .dockerignore excludes .env). Inject them as Railway
# environment variables. Persist the DB on a Railway volume mounted at /data and set
# TENTACLE_DB=/data/tentacle.db so learning survives restarts.
FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tentacle ./tentacle
COPY README.md CONTRACTS.md MOTHER.md ./

# Safe by default: unarmed, rung 0 = $0. It only shadow-logs until you arm + graduate.
CMD ["python", "-m", "tentacle", "run"]
