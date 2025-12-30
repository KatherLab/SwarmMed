import textwrap
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
import os
import json
import subprocess
from apps.logs.logger import get_logger
import shutil
import boto3
from botocore.exceptions import ClientError
from django.conf import settings
from apps.network.models import SwarmNetwork, UserCurrentNetwork
from .models import TrainingJob
from django.shortcuts import render
from django.http import JsonResponse
from .utils import download_s3_folder, get_s3_client
import time
from django.utils import timezone
from django.contrib import messages
from apps.project.models import Project, UserCurrentProject
from apps.results.models import TrainingResult
import tempfile

def get_user_project(request):
    """
    Get the current user's active project identifier.
    
    Args:
        request: Django request object
        
    Returns:
        tuple: (project_uuid, is_valid)
            - project_uuid: String UUID of the project or None
            - is_valid: Boolean indicating if a valid project was found
    """
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


@login_required(login_url='/users/signin/')
def training(request):
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/training/no_project_selected.html", {"segment": "training"})
    
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
        if not current_network or current_network.status != 'RUNNING':
            return render(request, "apps/training/no_network_started.html", {"segment": "training"})
    except UserCurrentNetwork.DoesNotExist:
        return render(request, "apps/training/no_network_started.html", {"segment": "training"})

    # Determine current training job and progress
    is_training_running = False
    training_job = None
    training_progress = 0
    training_status = 'Not started'
    training_logs = []

    if current_network:
        training_job = TrainingJob.objects.filter(network=current_network).order_by('-created_at').first()
        if training_job:
            training_status = training_job.status.title()
            if training_job.status == 'RUNNING':
                is_training_running = True
                # Derive progress from logs by counting completed rounds vs total rounds (num_rounds in server cfg)
                try:
                    total_rounds = 0
                    server_cfg_path = os.path.join('workspaces', str(training_job.project.identifier), str(current_network.identifier), 'job', 'app_server', 'config', 'config_fed_server.json')
                    if os.path.exists(server_cfg_path):
                        import json as _json
                        with open(server_cfg_path) as _f:
                            _d = _json.load(_f)
                            for wf in _d.get('workflows', []):
                                if wf.get('id') == 'swarm_controller':
                                    total_rounds = int(wf.get('args', {}).get('num_rounds', 0))
                                    break
                    rounds_finished = 0
                    workspace_dir = os.path.join('workspaces', str(training_job.project.identifier), str(current_network.identifier))
                    for root, _, files in os.walk(workspace_dir):
                        for fname in files:
                            if fname.startswith('log_fl') and fname.endswith('.txt'):
                                fpath = os.path.join(root, fname)
                                try:
                                    with open(fpath, 'r') as lf:
                                        for line in lf.readlines():
                                            if 'finished training round' in line:
                                                import re as _re
                                                m = _re.search(r'finished training round (\d+)', line)
                                                if m:
                                                    rnum = int(m.group(1))
                                                    if rnum > rounds_finished:
                                                        rounds_finished = rnum
                                except Exception:
                                    continue
                    if total_rounds > 0:
                        training_progress = min(100, int(rounds_finished * 100 / total_rounds))
                # If workflow ended or job executor finished successfully, force 100%
                    workspace_dir = os.path.join('workspaces', str(training_job.project.identifier), str(current_network.identifier))
                    ended = False
                    for root,_,files in os.walk(workspace_dir):
                        for fname in files:
                            if fname.startswith('log') and fname.endswith('.txt'):
                                fpath=os.path.join(root,fname)
                                try:
                                    with open(fpath,'r') as lf2:
                                        data=lf2.read()
                                        if 'ending workflow swarm_controller' in data or 'child worker process finished with RC 0' in data:
                                            ended=True
                                            break
                                except Exception:
                                    continue
                        if ended: break
                    if ended:
                        training_progress = 100
                        training_status = 'Completed'
                        is_training_running = False
                except Exception:
                    training_progress = 0
            elif training_job.status in ['COMPLETED', 'STOPPED', 'FAILED']:
                if training_job.status == 'COMPLETED':
                    training_progress = 100
    
            # Collect fl-client-1 logs (last 50 lines)
            training_logs = []
            try:
                if training_job:
                    base_workspace = os.path.join('workspaces', str(training_job.project.identifier), str(current_network.identifier), 'workspace', 'Test', 'prod_00', 'fl-client-1')
                    if os.path.isdir(base_workspace):
                        # find latest run folder
                        runs=[d for d in os.listdir(base_workspace) if os.path.isdir(os.path.join(base_workspace,d))]
                        if runs:
                            runs.sort(key=lambda d: os.path.getmtime(os.path.join(base_workspace,d)), reverse=True)
                            latest_run=os.path.join(base_workspace,runs[0])
                            # prefer log_fl.txt else log.txt
                            for cand in ['log_fl.txt','log.txt']:
                                log_file=os.path.join(latest_run,cand)
                                if os.path.exists(log_file):
                                    import re
                                    with open(log_file,'r') as lf:
                                        lines=lf.readlines()[-50:]
                                    for line in lines:
                                        line = line.strip()
                                        if not line: continue
                                        
                                        ts = ''; level = ''; msg = line; logger_name = ''
                                        ts_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                                        if ts_match:
                                            ts = ts_match.group(1)
                                            remaining = line[len(ts):].strip()
                                            dash_parts = remaining.split(' - ')
                                            if len(dash_parts) >= 3:
                                                logger_name = dash_parts[0].strip(' -')
                                                level = dash_parts[1].strip()
                                                msg = ' - '.join(dash_parts[2:])
                                            elif '\t' in remaining:
                                                tab_parts = remaining.split('\t')
                                                level = tab_parts[0].strip()
                                                msg = '\t'.join(tab_parts[1:])
                                            else:
                                                msg = remaining.strip(' -')

                                        if level:
                                            msg = re.sub(rf'^\s*-?\s*{level}\s*-?\s*', '', msg, flags=re.IGNORECASE)
                                        msg = re.sub(r'^\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s*', '', msg)
                                        
                                        training_logs.append({
                                            'timestamp': ts,
                                            'level': level,
                                            'message': msg.strip(),
                                            'logger': logger_name
                                        })
                                    break
            except Exception:
                training_logs = []
    context = {
        "segment": "training",
        "current_network": current_network,
        "is_training_running": is_training_running,
        "training_status": training_status,
        "training_progress": training_progress,
        "training_logs": training_logs,
    }
    return render(request, "apps/training/training.html", context)


@login_required(login_url='/users/signin/')
def start_training(request, network_id):
    """
    Prepares a FLARE app with the user's code and submits it as a job.
    """
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project
    logger = get_logger(user=request.user, project=project)

    # 1. Define paths
    job_dir = os.path.join('workspaces', str(project.identifier), str(network.identifier), 'job')
    app_custom_dir = os.path.join(job_dir, 'custom')
    source_code_prefix = f"{project.identifier}/code/training/"

    project_name = project.title.replace(' ', '_')
    admin_user_dir = os.path.join('/app', 'workspaces', str(project.identifier), str(network.identifier), 'workspace', project_name, 'prod_00', 'admin@nvidia.com') 

    # 2. Create app structure and download files
    os.makedirs(app_custom_dir, exist_ok=True)
    try:
        download_s3_folder(settings.AWS_STORAGE_BUCKET_NAME, source_code_prefix, app_custom_dir)
    except ClientError as e:
        logger.training.error(f"Failed to download training code from S3: {e}")

    # Build proper NVFLARE job structure:
    job_root = os.path.join('workspaces', str(project.identifier), str(network.identifier), 'job')
    app_server_dir = os.path.join(job_root, 'app_server')
    app_client_dir = os.path.join(job_root, 'app_client')
    app_server_cfg_dir = os.path.join(app_server_dir, 'config')
    app_client_cfg_dir = os.path.join(app_client_dir, 'config')
    app_client_custom_dir = os.path.join(app_client_dir, 'custom')

    os.makedirs(app_server_cfg_dir, exist_ok=True)
    os.makedirs(app_client_cfg_dir, exist_ok=True)
    os.makedirs(app_client_custom_dir, exist_ok=True)

    # Move downloaded code under app_client/custom (so BYOC code is inside the app)
    downloaded_custom_dir = os.path.join(job_root, 'custom')
    if os.path.isdir(downloaded_custom_dir):
        for root, _, files in os.walk(downloaded_custom_dir):
            for f in files:
                src = os.path.join(root, f)
                rel = os.path.relpath(src, downloaded_custom_dir)
                dst = os.path.join(app_client_custom_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
        shutil.rmtree(downloaded_custom_dir, ignore_errors=True)
        
    # Copy flare_adapter.py to app_client/custom
    # This ensures flare_adapter is available for direct import in training.py
    flare_adapter_src = os.path.join(settings.BASE_DIR, 'apps', 'training', 'flare_adapter.py')
    flare_adapter_dst = os.path.join(app_client_custom_dir, 'flare_adapter.py')
    if os.path.exists(flare_adapter_src):
        shutil.copyfile(flare_adapter_src, flare_adapter_dst)
    else:
        logger.training.error(f"flare_adapter.py not found at {flare_adapter_src}")

    # Copy .env file to app_client/custom for the job
    env_file_path = os.path.join(settings.BASE_DIR, '.env')
    if os.path.exists(env_file_path):
        shutil.copy(env_file_path, os.path.join(app_client_custom_dir, '.env'))
        logger.training.info("Copied .env file to job's custom directory.")
    else:
        logger.training.warning(f".env file not found at {env_file_path}, skipping copy to job directory.")

    # Inject the project_id into the training.py script
    training_py_path = os.path.join(app_client_custom_dir, 'training.py')
    if os.path.exists(training_py_path):
        with open(training_py_path, 'r') as f:
            training_script_content = f.read()
        
        # Replace the placeholder main() call with one that includes the project_id
        placeholder_main = 'main(project_id="default_project")'
        actual_main = f'main(project_id="{str(project.identifier)}")'
        
        if placeholder_main in training_script_content:
            training_script_content = training_script_content.replace(placeholder_main, actual_main)
            
            with open(training_py_path, 'w') as f:
                f.write(training_script_content)
            
            logger.training.info(f"Injected project_id '{str(project.identifier)}' into training.py")
        else:
            logger.training.warning(f"Could not find placeholder '{placeholder_main}' in training.py to inject project_id.")
    else:
        logger.training.warning(f"training.py not found at {training_py_path}, cannot inject project_id.")

    # Build meta.json based on current network participants
    if network.participants.filter(role='CLIENT').exists():
        client_names = list(network.participants.filter(role='CLIENT').values_list('participant_id', flat=True))
    else:
        client_names = ['fl-client-1', 'fl-client-2']

    meta = {
        "name": f"{project_name}_job",
        "deploy_map": {
            "app_server": ["server"],
            "app_client": client_names,
        }
    }
    with open(os.path.join(job_root, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    # Create placeholder config files if not present.
    server_custom_dir = os.path.join(app_server_dir, 'custom')
    os.makedirs(server_custom_dir, exist_ok=True)

    server_cfg = {
        "format_version": 2,
        "task_data_filters": [],
        "task_result_filters": [],
        "components": [],
        "workflows": [
            {
            "id": "swarm_controller",
            "path": "nvflare.app_common.ccwf.SwarmServerController",
            "args": {
                "num_rounds": 10
            }
            }
        ]
        }
    
    client_cfg = {
        "format_version": 2,
        "executors": [
            {
                "tasks": ["train"],
                "executor": {
                    "path": "nvflare.app_opt.pt.in_process_client_api_executor.PTInProcessClientAPIExecutor",
                    "args": {
                        "task_script_path": "custom/training.py"
                    }
                }
            },
            {
            "tasks": ["swarm_*"],
            "executor": {
                "path": "nvflare.app_common.ccwf.SwarmClientController",
                "args": {
                "learn_task_name": "train",
                "learn_task_timeout": 60.0,
                "persistor_id": "persistor",
                "aggregator_id": "aggregator",
                "shareable_generator_id": "shareable_generator",
                "min_responses_required": 2,
                "wait_time_after_min_resps_received": 1
                }
            }
            }
        ],
        "task_result_filters": [],
        "task_data_filters": [],
        "components": [
            {
            "id": "persistor",
            "path": "nvflare.app_opt.pt.file_model_persistor.PTFileModelPersistor",
            "args": {}
            },
            {
            "id": "shareable_generator",
            "name": "FullModelShareableGenerator",
            "args": {}
            },
            {
            "id": "aggregator",
            "name": "InTimeAccumulateWeightedAggregator",
            "args": {
                "expected_data_kind": "WEIGHTS"
            }
            },
            {
            "id": "model_selector",
            "name": "IntimeModelSelector",
            "args": {}
            }
        ]
        }
    with open(os.path.join(app_server_cfg_dir, 'config_fed_server.json'), 'w') as f:
        json.dump(server_cfg, f, indent=2)
    with open(os.path.join(app_client_cfg_dir, 'config_fed_client.json'), 'w') as f:
        json.dump(client_cfg, f, indent=2)

    logger.training.info(f"Prepared job at {job_root}")

    # Log files in admin_user_dir
    try:
        logger.training.info(f"Files in {admin_user_dir}: {os.listdir(admin_user_dir)}")
        with open(os.path.join(admin_user_dir, 'startup', 'fed_admin.json'), 'r') as f:
            logger.training.info(f"fed_admin.json content: {f.read()}")
    except Exception as e:
        logger.training.error(f"Could not list files in {admin_user_dir}: {e}")

    # 3. Submit the job using the FLARE API
    try:
        # Add a small delay if needed
        time.sleep(5)

        from nvflare.fuel.flare_api.flare_api import new_secure_session
        import socket
        
        job_path = os.path.join('/app', job_dir)

        # Wait for Overseer to be reachable inside the FLARE network
        for _ in range(60):
            try:
                with socket.create_connection(("overseer", 8443), timeout=2):
                    break
            except Exception:
                time.sleep(1)

        # Open secure session with the admin startup kit (cert auth)
        sess = new_secure_session(username='admin@nvidia.com', startup_kit_location=admin_user_dir, timeout=60.0)

        # Optional: sanity check connectivity
        # sys_info = sess.get_system_info()

        # Submit job
        rsp = sess.api.do_command(f"submit_job {job_path}") 
        logger.training.info(f"submit_job reply: {rsp}")
        
        job_id = None
        if isinstance(rsp, dict):
            job_id = rsp.get('job_id') or rsp.get('data') or str(rsp)
        else:
            job_id = getattr(rsp, 'job_id', None) or str(rsp)

        TrainingJob.objects.create(
            project=project,
            network=network,
            status='RUNNING' if job_id else 'FAILED',
            flare_job_id=job_id or 'unknown'
        )
        messages.success(request, f"Successfully submitted job {job_id}")
        
    except Exception as e:
        logger.training.error(f"Submit job via FLARE API failed: {e}", exc_info=True)
        TrainingJob.objects.create(
            project=project, 
            network=network, 
            status='FAILED', 
            flare_job_id='exception'
        )
        messages.error(request, f"Failed to submit job: {e}")

    return redirect('training')

@login_required(login_url='/users/signin/')
def stop_training(request, network_id):
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project
    logger = get_logger(user=request.user, project=project)

    running_job = TrainingJob.objects.filter(network=network, status='RUNNING').order_by('-created_at').first()

    if running_job:
        job_id = running_job.flare_job_id
        project_name = project.title.replace(' ', '_')
        admin_user_dir = os.path.join('/app', 'workspaces', str(project.identifier), str(network.identifier), 'workspace', project_name, 'prod_00', 'admin@nvidia.com')

        try:
            from nvflare.fuel.flare_api.flare_api import new_secure_session
            sess = new_secure_session(username='admin@nvidia.com', startup_kit_location=admin_user_dir)
            rsp = sess.api.do_command(f"abort_job {job_id}")
            logger.training.info(f"Abort job reply: {rsp}")
            running_job.status = 'STOPPED'
            running_job.save()
        except Exception as e:
            logger.training.error(f"Failed to abort job via FLARE API: {e}", exc_info=True)

        # Remove job folder
        job_dir = os.path.join('workspaces', str(project.identifier), str(network.identifier), 'job')
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
            logger.training.info(f"Removed job folder: {job_dir}")

    return redirect('training')


@login_required(login_url='/users/signin/')
def training_status_api(request):
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        return JsonResponse({"status": "No network", "progress": 0})

    status = 'Not started'
    progress = 0
    try:
        job = TrainingJob.objects.filter(network=current_network).order_by('-created_at').first()
        if not job:
            return JsonResponse({"status": status, "progress": progress})

        status = job.status.title()
        total_rounds = 0
        try:
            server_cfg_path = os.path.join('workspaces', str(job.project.identifier), str(current_network.identifier), 'job', 'app_server', 'config', 'config_fed_server.json')
            if os.path.exists(server_cfg_path):
                with open(server_cfg_path) as f:
                    cfg = json.load(f)
                    for wf in cfg.get('workflows', []):
                        if wf.get('id') == 'swarm_controller':
                            total_rounds = int(wf.get('args', {}).get('num_rounds', 0))
                            break
        except Exception:
            total_rounds = 0

        workspace_root = os.path.join('workspaces', str(job.project.identifier), str(current_network.identifier), 'workspace')
        rounds_finished = 0
        ended = False
        for client in ('fl-client-1', 'fl-client-2'):
            client_dir = None
            # find latest run dir under this client
            base_client = None
            for root, dirs, files in os.walk(workspace_root):
                if os.path.basename(root) == client:
                    base_client = root
                    break
            if base_client and os.path.isdir(base_client):
                runs=[d for d in os.listdir(base_client) if os.path.isdir(os.path.join(base_client,d))]
                if runs:
                    runs.sort(key=lambda d: os.path.getmtime(os.path.join(base_client,d)), reverse=True)
                    client_dir = os.path.join(base_client, runs[0])
            if not client_dir:
                continue
            for fname in ('log_fl.txt','log.txt'):
                fpath=os.path.join(client_dir,fname)
                if os.path.exists(fpath):
                    try:
                        with open(fpath,'r') as lf:
                            data=lf.read()
                            import re as _re
                            for m in _re.finditer(r'finished training round (\d+)', data):
                                r=int(m.group(1))
                                if r>rounds_finished:
                                    rounds_finished=r
                            if 'ending workflow swarm_controller' in data or 'child worker process finished with RC 0' in data:
                                ended=True
                    except Exception:
                        pass

        if total_rounds>0:
            progress=min(100, int(rounds_finished*100/total_rounds))
        if ended or progress>=100:
            progress=100
            status='Completed'
            # Update the database status if it's not already COMPLETED
            if job.status != 'COMPLETED':
                job.status = 'COMPLETED'
                job.completed_at = timezone.now()
                job.save()
        elif job.status=='RUNNING':
            status='Running'
    except Exception:
        pass
    return JsonResponse({"status": status, "progress": progress})

@login_required(login_url='/users/signin/')
def training_logs_api(request):
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        return JsonResponse({"logs": []})

    logs = []
    try:
        job = TrainingJob.objects.filter(network=current_network).order_by('-created_at').first()
        if not job:
            return JsonResponse({"logs": logs})
        base_workspace_root = os.path.join('workspaces', str(job.project.identifier), str(current_network.identifier), 'workspace')
        # find latest run under either client
        latest_log = None
        latest_mtime = -1
        for client in ('fl-client-1','fl-client-2'):
            base_client = None
            for root, dirs, files in os.walk(base_workspace_root):
                if os.path.basename(root) == client:
                    base_client = root
                    break
            if not base_client:
                continue
            runs=[d for d in os.listdir(base_client) if os.path.isdir(os.path.join(base_client,d))]
            if not runs:
                continue
            runs.sort(key=lambda d: os.path.getmtime(os.path.join(base_client,d)), reverse=True)
            latest_run=os.path.join(base_client, runs[0])
            for cand in ('log_fl.txt','log.txt'):
                log_path=os.path.join(latest_run, cand)
                if os.path.exists(log_path):
                    m=os.path.getmtime(log_path)
                    if m>latest_mtime:
                        latest_mtime=m
                        latest_log=log_path
        if latest_log and os.path.exists(latest_log):
            import re
            with open(latest_log,'r') as lf:
                lines=lf.readlines()[-100:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # Try to parse different formats
                # Format 1: 2025-12-30 11:43:53,610 - LoggerName - LEVEL - Message
                # Format 2: 2025-12-30 11:43:53,612\tLEVEL\tMessage
                
                ts = ''; level = ''; msg = line; logger_name = ''
                
                # Match timestamp at the beginning: 2025-12-30 11:43:53,610
                ts_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                if ts_match:
                    ts = ts_match.group(1)
                    remaining = line[len(ts):].strip()
                    
                    # Check for " - LoggerName - LEVEL - Message"
                    dash_parts = remaining.split(' - ')
                    if len(dash_parts) >= 3:
                        logger_name = dash_parts[0].strip(' -')
                        level = dash_parts[1].strip()
                        msg = ' - '.join(dash_parts[2:])
                    # Check for "\tLEVEL\tMessage"
                    elif '\t' in remaining:
                        tab_parts = remaining.split('\t')
                        level = tab_parts[0].strip()
                        msg = '\t'.join(tab_parts[1:])
                    else:
                        msg = remaining.strip(' -')

                # Clean up message: sometimes it starts with another level/timestamp
                # e.g. "INFO - Message" or "LEVEL Message"
                if level:
                    msg = re.sub(rf'^\s*-?\s*{level}\s*-?\s*', '', msg, flags=re.IGNORECASE)
                
                # Final clean for redundant timestamps in msg
                msg = re.sub(r'^\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s*', '', msg)
                
                logs.append({
                    'timestamp': ts,
                    'level': level,
                    'message': msg.strip(),
                    'logger': logger_name
                })
    except Exception:
        pass
    return JsonResponse({"logs": logs})
