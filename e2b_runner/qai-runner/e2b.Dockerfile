# Use the official Playwright Python image as a base
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Install standard testing libraries
RUN pip install --no-cache-dir \
    playwright \
    requests \
    locust \
    pytest \
    beautifulsoup4 \
    python-dotenv \
    jsonpath-ng

# Pre-install the Chromium browser
RUN python3 -m playwright install chromium
RUN python3 -m playwright install-deps chromium

# Set working directory
WORKDIR /home/user

# Copy locust template
COPY locustfile.py /home/user/locustfile.py

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
