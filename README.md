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



change css files locally during development in vs code new terminal

npm i
npm run build
npx tailwindcss -i ./static/assets/style.css -o ./static/dist/css/output.css --watch
npx webpack --watch