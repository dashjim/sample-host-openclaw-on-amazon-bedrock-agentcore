FROM public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# Install system tools needed for permission checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    iproute2 \
    curl \
    libcap2-bin \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# NOTE: Deliberately using default root user for this test.
# The whole point is to verify if AgentCore Runtime allows root.
# If AgentCore forces non-root, this test will reveal that.

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/ping || exit 1

CMD ["python", "-m", "privilege_separation_test"]
