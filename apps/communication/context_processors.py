"""
Context processors for the communication app.
These functions make certain data available globally in all templates
without having to pass them explicitly in every view.
"""

from django.db.models import Q
from .models import Message, ProjectPost, ProjectBoardAccess
from apps.project.models import Project


def unread_messages(request):
    """
    Calculates the total number of unread direct messages and new project board posts
    for the currently logged-in user.

    This is used to show notification badges in the navigation bar.
    """
    # If the user is not logged in, they don't have any messages
    if not request.user.is_authenticated:
        return {'unread_message_count': 0}

    user = request.user

    # 1. Direct Messages Count
    # We count messages where the current user is the recipient and 'is_read'
    # is False
    dm_count = Message.objects.filter(recipient=user, is_read=False).count()

    # 2. Project Board Posts Count
    # We need to find new posts in projects where the user is either the
    # author or a member
    user_projects = Project.objects.filter(
        Q(author=user) | Q(members=user)
    ).distinct()

    board_unread_count = 0

    for project in user_projects:
        try:
            # Check when the user last accessed this project's board
            access_log = ProjectBoardAccess.objects.get(
                user=user, project=project)
            last_accessed = access_log.last_accessed
        except ProjectBoardAccess.DoesNotExist:
            # If they have never accessed it, we consider all posts as
            # new/unread
            last_accessed = None

        if last_accessed:
            # Count posts created after the user's last access time
            count = ProjectPost.objects.filter(
                project=project,
                timestamp__gt=last_accessed
            ).count()
        else:
            # Count all posts in the project if the user has never clicked on
            # the board
            count = ProjectPost.objects.filter(project=project).count()

        board_unread_count += count

    # Combine both counts for the final notification badge number
    total_count = dm_count + board_unread_count

    return {'unread_message_count': total_count}
