# Base image
FROM python:3.10-slim

# Set the working directory
WORKDIR /app

# Install system dependencies
# Add any other required packages here
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install python packages
COPY ./requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . /app

# The entrypoint will be specified in docker-compose to start either the server or client
EXPOSE 8003 8002

