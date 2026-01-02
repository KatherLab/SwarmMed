"""
Management command to securely backup the database and media files to S3.
Usage: python manage.py secure_backup
"""

import os
import shutil
import subprocess
from datetime import datetime
from django.core.management.base import BaseCommand
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

class Command(BaseCommand):
    help = 'Creates a secure backup of the PostgreSQL database and uploads it to S3.'

    def handle(self, *args, **options):
        self.stdout.write("Starting secure backup...")

        now = datetime.now()
        timestamp = now.strftime('%Y%m%d_%H%M%S')
        backup_filename = f"db_backup_{timestamp}.sql"
        backup_path = os.path.join(settings.PROJECT_TEMP_DIR, backup_filename)

        # 1. Dump the Database
        db_settings = settings.DATABASES['default']
        env = os.environ.copy()
        env['PGPASSWORD'] = db_settings['PASSWORD']

        cmd = [
            'pg_dump',
            '-h', db_settings['HOST'],
            '-p', str(db_settings['PORT']),
            '-U', db_settings['USER'],
            '-F', 'c',  # Custom format (compressed)
            '-b',       # Include large objects (blobs)
            '-v',       # Verbose
            '-f', backup_path,
            db_settings['NAME']
        ]

        try:
            self.stdout.write(f"Dumping database to {backup_path}...")
            subprocess.run(cmd, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # 2. Upload to S3
            s3_backup_dir = "backups/"
            s3_path = os.path.join(s3_backup_dir, backup_filename)
            self.stdout.write(f"Uploading to S3: {s3_path}...")
            
            with open(backup_path, 'rb') as f:
                default_storage.save(s3_path, ContentFile(f.read()))
            
            self.stdout.write(self.style.SUCCESS(f"Backup uploaded successfully to {s3_path}"))

            # 3. Apply Retention Policy
            self.stdout.write("Applying retention policy...")
            retention_days = settings.BACKUP_RETENTION_DAYS
            directories, files = default_storage.listdir(s3_backup_dir)
            
            deleted_count = 0
            for filename in files:
                if not filename.startswith("db_backup_") or not filename.endswith(".sql"):
                    continue
                
                try:
                    # Extract timestamp: db_backup_20260102_120000.sql
                    ts_str = filename.replace("db_backup_", "").replace(".sql", "")
                    file_ts = datetime.strptime(ts_str, '%Y%m%d_%H%M%S')
                    
                    age = now - file_ts
                    if age.days >= retention_days:
                        self.stdout.write(f"Deleting old backup: {filename} (Age: {age.days} days)")
                        default_storage.delete(os.path.join(s3_backup_dir, filename))
                        deleted_count += 1
                except (ValueError, IndexError):
                    self.stdout.write(f"Skipping malformed backup filename: {filename}")
            
            if deleted_count > 0:
                self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_count} old backups."))
            else:
                self.stdout.write("No old backups found for deletion.")

        except subprocess.CalledProcessError as e:
            self.stdout.write(self.style.ERROR(f"pg_dump failed: {e.stderr.decode()}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Backup failed: {str(e)}"))
        finally:
            # 4. Cleanup local file
            if os.path.exists(backup_path):
                os.remove(backup_path)
                self.stdout.write("Local backup file cleaned up.")
