# Use the official Playwright Python image as a base
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

# Set global Playwright browser path
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV DISPLAY=:1

# Install GUI components for Live View
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    xvfb \
    fluxbox \
    x11vnc \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

# Install standard testing libraries
RUN pip install --no-cache-dir \
    playwright \
    requests \
    locust \
    pytest \
    beautifulsoup4 \
    python-dotenv \
    jsonpath-ng

# Pre-install the Chromium browser globally
RUN playwright install --with-deps chromium

# Set working directory
WORKDIR /home/user

# Copy locust template
COPY locustfile.py /home/user/locustfile.py

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
