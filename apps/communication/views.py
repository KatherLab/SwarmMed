"""Views for the communication app.
Contains the logic for displaying the chat dashboard, individual chat rooms,
and project discussion boards.
"""

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from logs import logger
from project.decorators import project_membership_required
from project.models import Project

from .forms import ProjectPostForm
from .models import Message, ProjectBoardAccess, ProjectPost


@login_required
def chat_dashboard(request):
    """Displays the main communication hub.

    Lists existing direct message conversations and active project boards.

    Args:
        request (HttpRequest): The HTTP request object.

    Returns:
        HttpResponse: The rendered chat dashboard page.
    """
    user = request.user

    # --- 1. Available Users for New Chat ---
    # Find all users currently in projects with the current user efficiently
    user_projects = Project.objects.filter(
        Q(author=user) | Q(members=user)
    ).distinct()

    available_users = (
        User.objects.filter(
            Q(created_projects__in=user_projects)
            | Q(member_projects__in=user_projects)
        )
        .exclude(id=user.id)
        .distinct()
    )

    # --- 2. Direct Messages Logic ---
    # Find everyone this user has interacted with
    contact_users = (
        User.objects.filter(
            Q(received_messages__sender=user)
            | Q(sent_messages__recipient=user)
        )
        .exclude(id=user.id)
        .distinct()
        .select_related("profile")
    )

    # Subquery to get the ID of the most recent message between the user and each contact
    last_msg_subquery = (
        Message.objects.filter(
            Q(sender=user, recipient=OuterRef("pk"))
            | Q(sender=OuterRef("pk"), recipient=user)
        )
        .order_by("-created_at")
        .values("id")[:1]
    )

    # Annotate contacts with unread count and the ID of the last message
    contact_users = contact_users.annotate(
        unread_count_annotated=Count(
            "sent_messages",
            filter=Q(
                sent_messages__recipient=user, sent_messages__is_read=False
            ),
        ),
        last_msg_id=Subquery(last_msg_subquery),
    )

    # Bulk fetch the last messages to avoid N+1 in the loop
    last_msg_ids = [c.last_msg_id for c in contact_users if c.last_msg_id]
    messages_dict = {
        m.id: m for m in Message.objects.filter(id__in=last_msg_ids)
    }

    contacts = []
    for contact in contact_users:
        last_msg = messages_dict.get(contact.last_msg_id)
        contacts.append(
            {
                "user": contact,
                "last_message": last_msg,
                "unread_count": contact.unread_count_annotated,
            }
        )

    # Sort the list of contacts so the most recent conversation is at the top
    contacts.sort(
        key=lambda x: (
            x["last_message"].created_at
            if x["last_message"]
            else timezone.now()
        ),
        reverse=True,
    )

    # --- 3. Project Boards Logic ---
    # Subquery for the latest post ID on each project board
    last_post_subquery = (
        ProjectPost.objects.filter(project=OuterRef("pk"))
        .order_by("-created_at")
        .values("id")[:1]
    )

    # Subquery for the user's last access time to each project board
    last_access_subquery = ProjectBoardAccess.objects.filter(
        user=user, project=OuterRef("pk")
    ).values("updated_at")[:1]

    # Annotate project queryset with necessary metadata
    # We use Case/When to handle the two unread count scenarios:
    # 1. User has visited before: Count posts newer than last_accessed_val
    # 2. User has never visited: Count all posts
    user_projects_annotated = (
        user_projects.select_related("author")
        .annotate(
            last_post_id=Subquery(last_post_subquery),
            last_accessed_val=Subquery(last_access_subquery),
        )
        .annotate(
            unread_count_calculated=Count(
                "posts",
                filter=Q(posts__created_at__gt=F("last_accessed_val"))
                | Q(last_accessed_val__isnull=True),
            )
        )
    )

    # Bulk fetch last posts
    last_post_ids = [
        p.last_post_id for p in user_projects_annotated if p.last_post_id
    ]
    posts_dict = {
        p.id: p for p in ProjectPost.objects.filter(id__in=last_post_ids)
    }

    project_boards = []
    for project in user_projects_annotated:
        # Unread count is now pre-calculated in the database query
        unread_posts = project.unread_count_calculated

        project_boards.append(
            {
                "project": project,
                "unread_count": unread_posts,
                "last_post": posts_dict.get(project.last_post_id),
            }
        )

    context = {
        "segment": "communication",
        "contacts": contacts,
        "project_boards": project_boards,
        "available_users": available_users,
    }
    return render(request, "apps/communication/chat_dashboard.html", context)


@login_required
def chat_room(request, user_id):
    """Displays the conversation history with a specific user and processes new messages.

    Args:
        request (HttpRequest): The HTTP request object.
        user_id (int): The ID of the other user in the conversation.

    Returns:
        HttpResponse: The rendered chat room page or a redirect after sending a message.
    """
    other_user = get_object_or_404(User, pk=user_id)
    user = request.user
    log = logger.get_logger(user=user)

    # Log access to the chat room for audit purposes
    log.access.info(
        f"User accessed direct message room with user: {other_user.username}",
        target_user=other_user.username,
    )

    # Automatically mark all incoming messages from this user as read
    Message.objects.filter(
        sender=other_user, recipient=user, is_read=False
    ).update(is_read=True)

    # Invalidate unread count cache for the current user
    cache.delete(f"unread_messages_count_{user.id}")

    # Handle sending a new message
    if request.method == "POST":
        body = request.POST.get("body")
        if body:
            Message.objects.create(
                sender=user,
                recipient=other_user,
                subject="Chat Message",  # Default subject for simple chat
                body=body,
            )
            log.access.info(
                f"User sent direct message to: {other_user.username}",
                target_user=other_user.username,
            )
            # Redirect back to the same page to prevent double-submission on
            # refresh
            return redirect("communication:chat_room", user_id=user_id)

    # Retrieve the full message history between these two users
    messages_history = (
        Message.objects.filter(
            Q(sender=user, recipient=other_user)
            | Q(sender=other_user, recipient=user)
        )
        .select_related("sender", "recipient")
        .order_by("created_at")
    )

    context = {
        "segment": "communication",
        "other_user": other_user,
        "messages_history": messages_history,
    }
    return render(request, "apps/communication/chat_room.html", context)


@login_required
@project_membership_required
def project_board(request, project_id):
    """Displays the discussion board for a specific project.

    Allows members to post updates and questions.

    Args:
        request (HttpRequest): The HTTP request object.
        project_id (int): The ID of the project whose board is being accessed.

    Returns:
        HttpResponse: The rendered project board page or a redirect after creating a post.
    """
    project = get_object_or_404(Project, pk=project_id)
    log = logger.get_logger(user=request.user, project=project)

    # Log access to the project discussion board
    log.access.info(
        f"User accessed discussion board for project: {project.title} ({project.identifier})"
    )

    # Update the user's access log for this board to mark current posts as
    # 'read'
    ProjectBoardAccess.objects.update_or_create(
        user=request.user, project=project
    )

    # Handle a new post submission
    if request.method == "POST":
        form = ProjectPostForm(request.POST)
        if form.is_valid():
            post = form.save(commit=False)
            post.project = project
            post.author = request.user
            post.save()

            log.access.info(
                f"User created new post on discussion board for project: {project.title}"
            )

            # Update access log again so the user's own new post isn't
            # immediately flagged as 'new' for them.
            ProjectBoardAccess.objects.update_or_create(
                user=request.user, project=project
            )
            return redirect(
                "communication:project_board", project_id=project.id
            )
    else:
        # Provide a blank form for GET requests
        form = ProjectPostForm()

    # Fetch all posts belonging to this project
    posts = project.posts.select_related("author").all()

    context = {
        "segment": "project",
        "project": project,
        "posts": posts,
        "form": form,
    }
    return render(request, "apps/communication/project_board.html", context)
