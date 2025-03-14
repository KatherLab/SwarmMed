1. Build and Start Containers:
Run the following command:
bash


docker compose up --build
2. Create the Bucket:
Open the MinIO web console at http://localhost:9001 with:
Username: minio_access_key
Password: minio_secret_key
Create a bucket called medical-data matching the AWS_STORAGE_BUCKET_NAME in your settings.
4. Tailwind in Development:
To use auto-reloading of Tailwind CSS (which is enabled via django-browser-reload), you can run:
bash


python manage.py tailwind start
This will watch for changes in your Tailwind configuration/templates and update CSS automatically.
4. Upload Files:
Open http://localhost:8000 in your browser. Use the form to upload a file—the file will be sent to your MinIO bucket via the S3 API.