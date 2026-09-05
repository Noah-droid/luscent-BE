#!/bin/bash
# EC2 Deployment Script for QAI Backend
# Run this on your EC2 instance after cloning the repo

set -e

echo "=== QAI Backend EC2 Deployment ==="

# 1. Install Docker & Docker Compose
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Docker installed. Please log out and back in, then re-run."
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "Installing Docker Compose..."
    sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
fi

# 2. Copy env file
if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
    echo "EDIT .env with your actual values before continuing!"
    exit 1
fi

# 3. Choose deployment mode
echo "Select deployment mode:"
echo "1) API only (with external Neon DB + local Redis container)"
echo "2) API + Celery (with external DB + local Redis container)"
echo "3) Full stack (API + Celery + Runner)"
read -p "Choice [1-3]: " choice

case $choice in
    1)
        PROFILES="--profile api"
        ;;
    2)
        PROFILES="--profile api --profile celery"
        ;;
    3)
        PROFILES="--profile api --profile celery --profile runner"
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

# 4. Build and start
echo "Building and starting containers..."
docker-compose $PROFILES up -d --build

# 5. Run migrations
echo "Running migrations..."
docker-compose exec api python manage.py migrate

# 6. Create superuser (optional)
read -p "Create Django superuser? (y/n): " create_su
if [ "$create_su" = "y" ]; then
    docker-compose exec api python manage.py createsuperuser
fi

echo "=== Deployment Complete ==="
echo "API: http://$(curl -s ifconfig.me):8000"
echo "Runner: http://$(curl -s ifconfig.me):8080"
echo ""
echo "To view logs: docker-compose logs -f"
echo "To stop: docker-compose down"