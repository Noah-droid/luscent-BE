# Use the official Playwright Python image as a base
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

# Set global Playwright browser path to ensure availability for all users
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install standard testing libraries
RUN pip install --no-cache-dir \
    playwright \
    requests \
    locust \
    pytest \
    beautifulsoup4 \
    python-dotenv \
    jsonpath-ng

# Pre-install the Chromium browser and its system dependencies to the global path
RUN playwright install --with-deps chromium

# Set working directory
WORKDIR /home/user

# Copy locust template
COPY locustfile.py /home/user/locustfile.py

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
