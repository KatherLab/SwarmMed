# Stage 1: Build static assets
FROM node:20-slim AS static-builder
WORKDIR /app
COPY package.json package-lock.json postcss.config.js tailwind.config.js webpack.config.js ./
COPY static ./static
COPY templates ./templates
RUN npm ci && npm run build

# Stage 2: Final image
FROM python:3.12-slim-bookworm

# set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    gosu \
    netcat-openbsd \
    libpq-dev \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch="$(dpkg --print-architecture)" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian "$( . /etc/os-release && echo "$VERSION_CODENAME")" stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends \
    docker-ce-cli \
    docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# install python dependencies with uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && uv pip sync --system requirements.txt

COPY . .
# Copy built assets from Stage 1
COPY --from=static-builder /app/static/dist ./static/dist

# Create a non-root user and set permissions
RUN useradd -m appuser && chown -R appuser:appuser /app

# Add entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Use entrypoint script
ENTRYPOINT ["/entrypoint.sh"]
