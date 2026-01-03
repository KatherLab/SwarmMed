import multiprocessing
import os

# Dynamic worker calculation: 2 * CPU cores + 1
# Can be overridden with GUNICORN_WORKERS environment variable
workers = int(os.environ.get('GUNICORN_WORKERS', 2 * multiprocessing.cpu_count() + 1))

bind = '0.0.0.0:8000'
threads = 8
worker_class = 'gthread'
worker_connections = 1000
accesslog = '-'
errorlog = '-'
loglevel = 'info'
capture_output = True
enable_stdio_inheritance = True
timeout = 120
keepalive = 5

# Restart workers after handling this many requests (prevents memory leaks)
max_requests = 1000
max_requests_jitter = 50

# Use shared memory for worker tmp directory (faster)
worker_tmp_dir = '/dev/shm'

# Preload application for better performance
preload_app = True
