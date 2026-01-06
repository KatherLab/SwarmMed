import base64
import hashlib
import os
import shutil

# Bandit B404: subprocess is required for pg_* client tooling; no shell=True usage.
import subprocess  # nosec B404
import tarfile
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from storages.backends.s3boto3 import S3Boto3Storage

from .models import BackupStatus, StorageBackend


def _is_within_directory(base_path, target_path):
    base = os.path.realpath(base_path)
    target = os.path.realpath(target_path)
    return target == base or target.startswith(f"{base}{os.sep}")


def safe_extract_tar(archive, destination):
    """Safely extract tar files without allowing traversal or symlink abuse."""
    dest = Path(destination).resolve()
    for member in archive.getmembers():
        member_path = dest / member.name
        if member.islnk() or member.issym():
            raise Exception(
                f"Refusing to extract symbolic link '{member.name}' from backup"
            )
        if not _is_within_directory(dest, member_path):
            raise Exception(
                f"Archive member '{member.name}' would extract outside of {dest}. Aborting restore."
            )
    # Bandit B202: members are validated for traversal and symlinks above.
    archive.extractall(path=dest)  # nosec B202


def get_fernet():
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.BACKUP_ENCRYPTION_KEY.encode()).digest()
    )
    return Fernet(key)


def get_backup_storage(config):
    """
    Returns a storage instance for the backup destination.
    If custom S3 credentials are provided, returns a custom S3Boto3Storage.
    Otherwise, returns the default storage.
    """
    if config.storage_backend == StorageBackend.S3:
        if config.s3_access_key_id and config.s3_secret_access_key:
            return S3Boto3Storage(
                access_key=config.s3_access_key_id,
                secret_key=config.s3_secret_access_key,
                bucket_name=config.s3_bucket
                or settings.AWS_STORAGE_BUCKET_NAME,
                endpoint_url=config.s3_endpoint_url
                or settings.AWS_S3_ENDPOINT_URL,
                region_name=config.s3_region_name
                or settings.AWS_S3_REGION_NAME,
            )
    return default_storage


def check_postgres_tools():
    """
    Verifies that necessary PostgreSQL client tools are available in the system PATH.
    """
    tools = ["pg_dump", "pg_restore", "psql", "createdb", "dropdb"]
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise Exception(
            f"Missing PostgreSQL client tools: {', '.join(missing)}"
        )


def backup_database(db_alias, output_path):
    check_postgres_tools()
    db_settings = settings.DATABASES[db_alias]
    env = os.environ.copy()
    env["PGPASSWORD"] = db_settings["PASSWORD"]

    cmd = [
        "pg_dump",
        "-h",
        db_settings["HOST"],
        "-p",
        str(db_settings["PORT"]),
        "-U",
        db_settings["USER"],
        "-F",
        "c",
        "-b",
        "-f",
        output_path,
        db_settings["NAME"],
    ]
    try:
        # Bandit B603: args are a fixed list; shell=False; inputs come from settings.
        subprocess.run(
            cmd, env=env, check=True, capture_output=True
        )  # nosec B603
    except subprocess.CalledProcessError as e:
        raise Exception(f"pg_dump failed: {e.stderr.decode()}") from e


def restore_database(db_alias, input_path):
    check_postgres_tools()
    db_settings = settings.DATABASES[db_alias]
    env = os.environ.copy()
    env["PGPASSWORD"] = db_settings["PASSWORD"]

    # Drop and recreate database to ensure a clean restore
    terminate_cmd = [
        "psql",
        "-h",
        db_settings["HOST"],
        "-p",
        str(db_settings["PORT"]),
        "-U",
        db_settings["USER"],
        "-d",
        "postgres",
        "-v",
        f"dbname={db_settings['NAME']}",
        "-c",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :'dbname' AND pid <> pg_backend_pid();",
    ]
    # We don't check=True here because it might fail if the DB doesn't exist yet, which is fine
    # Bandit B603: args are a fixed list; shell=False; inputs come from settings.
    subprocess.run(
        terminate_cmd, env=env, check=False, capture_output=True
    )  # nosec B603

    drop_cmd = [
        "dropdb",
        "-h",
        db_settings["HOST"],
        "-p",
        str(db_settings["PORT"]),
        "-U",
        db_settings["USER"],
        "--if-exists",
        db_settings["NAME"],
    ]
    # Bandit B603: args are a fixed list; shell=False; inputs come from settings.
    subprocess.run(
        drop_cmd, env=env, check=True, capture_output=True
    )  # nosec B603

    create_cmd = [
        "createdb",
        "-h",
        db_settings["HOST"],
        "-p",
        str(db_settings["PORT"]),
        "-U",
        db_settings["USER"],
        db_settings["NAME"],
    ]
    # Bandit B603: args are a fixed list; shell=False; inputs come from settings.
    subprocess.run(
        create_cmd, env=env, check=True, capture_output=True
    )  # nosec B603

    restore_cmd = [
        "pg_restore",
        "-h",
        db_settings["HOST"],
        "-p",
        str(db_settings["PORT"]),
        "-U",
        db_settings["USER"],
        "-d",
        db_settings["NAME"],
        "-v",
        input_path,
    ]
    try:
        # Bandit B603: args are a fixed list; shell=False; inputs come from settings.
        subprocess.run(
            restore_cmd, env=env, check=True, capture_output=True
        )  # nosec B603
    except subprocess.CalledProcessError as e:
        raise Exception(f"pg_restore failed: {e.stderr.decode()}") from e


def is_same_s3_destination(config):
    """
    Checks if the backup destination is the same as the source S3 bucket.
    """
    if config.storage_backend != StorageBackend.S3:
        return False

    conf_endpoint = config.s3_endpoint_url or settings.AWS_S3_ENDPOINT_URL
    sys_endpoint = settings.AWS_S3_ENDPOINT_URL

    conf_bucket = config.s3_bucket or settings.AWS_STORAGE_BUCKET_NAME
    sys_bucket = settings.AWS_STORAGE_BUCKET_NAME

    return conf_endpoint == sys_endpoint and conf_bucket == sys_bucket


def backup_s3_storage(target_dir, exclude_prefix):
    """
    Downloads all objects from the default system S3 storage to a local directory.
    """
    s3_dir = os.path.join(target_dir, "s3_storage")
    os.makedirs(s3_dir, exist_ok=True)

    def walk_s3(path):
        dirs, filenames = default_storage.listdir(path)
        for filename in filenames:
            full_s3_path = os.path.join(path, filename)

            # Skip the backup directory itself if it's in the same bucket
            if exclude_prefix and full_s3_path.startswith(exclude_prefix):
                continue

            local_file_path = os.path.join(s3_dir, full_s3_path)
            os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

            with default_storage.open(full_s3_path, "rb") as s3_file:
                with open(local_file_path, "wb") as local_file:
                    local_file.write(s3_file.read())

        for directory in dirs:
            if directory:
                walk_s3(os.path.join(path, directory))

    walk_s3("")


def restore_s3_storage(source_dir):
    """
    Uploads all objects from a local directory back to the default system S3 storage.
    """
    s3_src_dir = os.path.join(source_dir, "s3_storage")
    if not os.path.exists(s3_src_dir):
        return

    for root, _dirs, files in os.walk(s3_src_dir):
        for filename in files:
            local_path = os.path.join(root, filename)
            s3_path = os.path.relpath(local_path, s3_src_dir)

            with open(local_path, "rb") as local_file:
                if default_storage.exists(s3_path):
                    default_storage.delete(s3_path)
                default_storage.save(s3_path, ContentFile(local_file.read()))


def perform_backup(log_obj):
    config = log_obj.config
    temp_dir = os.path.join(settings.PROJECT_TEMP_DIR, str(log_obj.identifier))
    os.makedirs(temp_dir, exist_ok=True)

    try:
        log_obj.status = BackupStatus.RUNNING
        log_obj.save()

        archive_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        work_dir = os.path.join(temp_dir, archive_name)
        os.makedirs(work_dir, exist_ok=True)

        # 1. Databases
        if config.include_databases:
            db_dir = os.path.join(work_dir, "databases")
            os.makedirs(db_dir, exist_ok=True)
            for db_alias in settings.DATABASES:
                db_path = os.path.join(db_dir, f"{db_alias}.sql")
                backup_database(db_alias, db_path)
                if db_alias not in log_obj.included_databases:
                    log_obj.included_databases.append(db_alias)

        # 2. Media (Local)
        if config.include_media:
            media_dir = os.path.join(work_dir, "media")
            if os.path.exists(settings.MEDIA_ROOT):
                shutil.copytree(settings.MEDIA_ROOT, media_dir)
                log_obj.has_media = True

        # 3. S3 / MinIO Storage (System-wide)
        if config.include_s3_storage:
            # We only exclude the prefix if we are backing up to the SAME storage
            exclude_prefix = (
                config.s3_prefix if is_same_s3_destination(config) else None
            )
            backup_s3_storage(work_dir, exclude_prefix)
            log_obj.has_s3_storage = True

        # 4. Create Tarball
        tar_path = os.path.join(temp_dir, f"{archive_name}.tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(work_dir, arcname=archive_name)

        # 5. Encrypt
        fernet = get_fernet()
        enc_path = tar_path + ".enc"
        with open(tar_path, "rb") as f_in, open(enc_path, "wb") as f_out:
            encrypted_data = fernet.encrypt(f_in.read())
            f_out.write(encrypted_data)

        # 6. Store in Destination
        filename = os.path.basename(enc_path)
        log_obj.filename = filename
        log_obj.file_size = os.path.getsize(enc_path)

        if config.storage_backend == StorageBackend.S3:
            storage = get_backup_storage(config)
            s3_path = os.path.join(config.s3_prefix, filename)
            with open(enc_path, "rb") as f:
                storage.save(s3_path, ContentFile(f.read()))

            bucket = config.s3_bucket or settings.AWS_STORAGE_BUCKET_NAME
            endpoint = config.s3_endpoint_url or settings.AWS_S3_ENDPOINT_URL
            log_obj.storage_location = f"{endpoint}/{bucket}/{s3_path}"
        else:
            final_path = os.path.join(config.local_path, filename)
            os.makedirs(config.local_path, exist_ok=True)
            shutil.copy2(enc_path, final_path)
            log_obj.storage_location = final_path

        log_obj.status = BackupStatus.SUCCESS
        log_obj.finished_at = timezone.now()
        log_obj.save()

    except Exception as e:
        log_obj.status = BackupStatus.FAILED
        log_obj.error_message = str(e)
        log_obj.save()
        raise e
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def perform_restore(log_obj):
    config = log_obj.config
    temp_dir = os.path.join(
        settings.PROJECT_TEMP_DIR, f"restore_{log_obj.identifier}"
    )
    os.makedirs(temp_dir, exist_ok=True)

    try:
        log_obj.status = BackupStatus.RESTORING
        log_obj.save()

        # 1. Retrieve file from Source
        # Security: Ensure filename is just a basename to prevent traversal
        safe_filename = os.path.basename(log_obj.filename)
        enc_path = os.path.join(temp_dir, safe_filename)

        if config.storage_backend == StorageBackend.S3:
            storage = get_backup_storage(config)
            s3_path = os.path.join(config.s3_prefix, log_obj.filename)
            with (
                storage.open(s3_path, "rb") as f_in,
                open(enc_path, "wb") as f_out,
            ):
                f_out.write(f_in.read())
        else:
            shutil.copy2(log_obj.storage_location, enc_path)

        # 2. Decrypt
        fernet = get_fernet()
        tar_path = enc_path.replace(".enc", "")
        with open(enc_path, "rb") as f_in, open(tar_path, "wb") as f_out:
            decrypted_data = fernet.decrypt(f_in.read())
            f_out.write(decrypted_data)

        # 3. Extract
        with tarfile.open(tar_path, "r:gz") as tar:
            safe_extract_tar(tar, temp_dir)

        extract_dir = None
        for item in os.listdir(temp_dir):
            if item.startswith("backup_") and os.path.isdir(
                os.path.join(temp_dir, item)
            ):
                extract_dir = os.path.join(temp_dir, item)
                break

        if not extract_dir:
            raise Exception("Could not find backup content in archive.")

        # 4. Restore Databases
        db_dir = os.path.join(extract_dir, "databases")
        if os.path.exists(db_dir):
            for db_file in os.listdir(db_dir):
                db_alias = db_file.replace(".sql", "")
                if db_alias in settings.DATABASES:
                    restore_database(db_alias, os.path.join(db_dir, db_file))

        # 5. Restore Media (Local)
        media_dir = os.path.join(extract_dir, "media")
        if os.path.exists(media_dir):
            if os.path.exists(settings.MEDIA_ROOT):
                shutil.rmtree(settings.MEDIA_ROOT)
            shutil.copytree(media_dir, settings.MEDIA_ROOT)

        # 6. Restore S3 Storage (System-wide)
        if log_obj.has_s3_storage:
            restore_s3_storage(extract_dir)

        log_obj.status = BackupStatus.SUCCESS
        log_obj.save()

    except Exception as e:
        log_obj.status = BackupStatus.FAILED
        log_obj.error_message = f"Restore failed: {str(e)}"
        log_obj.save()
        raise e
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
