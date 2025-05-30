# Import 
    from apps.logs import logger

# simple usage
    log = logger.get_logger()
    # Log different levels with categories
    log.project.info("User accessed dashboard")
    log.data.info("File uploaded", filename="data.csv", size="1.5MB")
    log.network.warning("Slow network detected", latency="500ms")
    log.training.error("Model training failed", error="Out of memory")
    log.results.info("Analysis complete", accuracy="95%")

# manual context management
    from apps.logs import logger
    from apps.logs.context import set_context
    from django.contrib.auth.models import User
    from apps.project.models import Project

    # Set context manually
    user = User.objects.get(id=1)
    project = Project.objects.get(identifier='my-project')
    set_context(user=user, project=project)

    log = logger.get_logger()
    log.training.info("Batch processing started")

# explicit context management
    log = logger.get_logger()

    # Use automatic context
    log.data.info("Using automatic context")

    # Override context for this log only
    different_user = User.objects.get(id=2)
    different_project = Project.objects.get(identifier='other-project')

    log.data.info("Using different context", 
                user=different_user, 
                project=different_project,
                operation="cross-project-sync")