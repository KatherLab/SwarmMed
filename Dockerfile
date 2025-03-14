# Use a lightweight Python image
FROM python:3.9-slim

# Prevent Python from writing bytecode and enable unbuffered output.
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /code

# Install system dependencies including Node.js, npm, and build-essential
RUN apt-get update && apt-get install -y nodejs npm build-essential

# Copy Python dependencies and install them
COPY requirements.txt /code/
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the entire project into the container
COPY . /code/

# (Optional) Debug: list the theme directory
RUN ls -la /code/theme

# Expose port 8000
EXPOSE 8000

# Run migrations, build Tailwind assets, then start the app using gunicorn
CMD ["sh", "-c", "python manage.py collectstatic --noinput && python manage.py makemigrations && python manage.py migrate && gunicorn medapp.wsgi:application --bind 0.0.0.0:8000 && python manage.py tailwind start"]