"""
Context processors for the communication app.
These functions make certain data available globally in all templates
without having to pass them explicitly in every view.
"""

from django.core.cache import cache
from django.db.models import Q, Count, OuterRef, Subquery, F
from django.db.models.functions import Coalesce
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
    cache_key = f'unread_messages_count_{user.id}'

    # Try to get from cache
    total_count = cache.get(cache_key)
    if total_count is not None:
        return {'unread_message_count': total_count}

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

    # Subquery for the user's last access time to each project board
    last_access_subquery = ProjectBoardAccess.objects.filter(
        user=user,
        project=OuterRef('pk')
    ).values('last_accessed')[:1]

    # Calculate unread posts per project in a single query
    # Logic:
    # - If last_accessed exists: count posts where timestamp > last_accessed
    # - If last_accessed is null: count all posts
    
    projects_with_counts = user_projects.annotate(
        last_accessed_val=Subquery(last_access_subquery)
    ).annotate(
        unread_count=Count(
            'posts',
            filter=Q(
                posts__timestamp__gt=F('last_accessed_val')
            ) | Q(
                last_accessed_val__isnull=True
            )
        )
    )

    # Sum up the unread counts from all projects
    board_unread_count = sum(p.unread_count for p in projects_with_counts)

    # Combine both counts for the final notification badge number
    total_count = dm_count + board_unread_count
    
    # Cache the result for 5 minutes (300 seconds)
    cache.set(cache_key, total_count, 300)

    return {'unread_message_count': total_count}
