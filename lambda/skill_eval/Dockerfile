FROM public.ecr.aws/lambda/python:3.12-arm64

# Install Node.js 22 (for Claude CLI)
RUN dnf install -y tar gzip xz && \
    curl -fsSL https://nodejs.org/dist/v22.16.0/node-v22.16.0-linux-arm64.tar.xz | \
    tar -xJ --strip-components=1 -C /usr/local && \
    dnf clean all

# Install Claude CLI
RUN npm install -g @anthropic-ai/claude-code@latest 2>/dev/null; exit 0

# Install skill-eval from local source (pre-cloned, no git needed)
COPY skill_eval_src/ /tmp/skill_eval_src/
RUN pip install --no-cache-dir /tmp/skill_eval_src/ pyyaml boto3 && \
    rm -rf /tmp/skill_eval_src

# Copy handler
COPY handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
