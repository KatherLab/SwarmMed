source venv/bin/activate

deactivate

docker compose build
docker compose up


remove in production in docker-compose.yml:
volumes:
      - ./:/app
    ports:
      - "8000:8000"
command: python manage.py runserver 0.0.0.0:8000