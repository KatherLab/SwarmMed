from django.apps import AppConfig


class DataConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.data'
    
    def ready(self):
        from .utils import create_minio_bucket
        create_minio_bucket('mediswarmcloud')
