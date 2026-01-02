"""
Views for the communication app.
Contains the logic for displaying the chat dashboard, individual chat rooms,
and project discussion boards.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Q
from django.utils import timezone

from .models import Message, ProjectPost, ProjectBoardAccess
from .forms import ProjectPostForm
from apps.project.models import Project


@login_required
def chat_dashboard(request):
    """
    Displays the main communication hub.
    Lists existing direct message conversations and active project boards.
    """
    user = request.user

    # --- 1. Direct Messages Logic ---
    # Find IDs of everyone this user has interacted with
    sent_to = Message.objects.filter(
        sender=user).values_list(
        'recipient',
        flat=True)
    received_from = Message.objects.filter(
        recipient=user).values_list(
        'sender', flat=True)

    # Combine and get unique user IDs
    contact_ids = set(list(sent_to) + list(received_from))

    contacts = []
    for contact_id in contact_ids:
        # Fetch the contact user object
        contact = User.objects.get(id=contact_id)

        # Get the most recent message between these two users
        last_msg = Message.objects.filter(Q(sender=user, recipient=contact) | Q(
            sender=contact, recipient=user)).order_by('-timestamp').first()

        # Count how many messages from this contact are currently unread
        unread_count = Message.objects.filter(
            sender=contact,
            recipient=user,
            is_read=False
        ).count()

        contacts.append({
            'user': contact,
            'last_message': last_msg,
            'unread_count': unread_count
        })

    # Sort the list of contacts so the most recent conversation is at the top
    contacts.sort(
        key=lambda x: x['last_message'].timestamp if x['last_message'] else None,
        reverse=True)

    # --- 2. Project Boards Logic ---
    # Get all projects the user is involved in
    user_projects = Project.objects.filter(
        Q(author=user) | Q(members=user)
    ).distinct()

    project_boards = []
    for project in user_projects:
        # Check for unread posts based on the last time the user clicked the
        # board
        try:
            access_log = ProjectBoardAccess.objects.get(
                user=user, project=project)
            last_accessed = access_log.last_accessed
        except ProjectBoardAccess.DoesNotExist:
            last_accessed = None

        if last_accessed:
            unread_posts = ProjectPost.objects.filter(
                project=project,
                timestamp__gt=last_accessed
            ).count()
        else:
            unread_posts = ProjectPost.objects.filter(project=project).count()

        # Get the latest update on the project board
        last_post = ProjectPost.objects.filter(
            project=project
        ).order_by('-timestamp').first()

        project_boards.append({
            'project': project,
            'unread_count': unread_posts,
            'last_post': last_post
        })

    # --- 3. Available Users for New Chat ---
    # Find all users currently in projects with the current user
    available_users = set()
    for p in user_projects:
        available_users.add(p.author)
        for m in p.members.all():
            available_users.add(m)

    # Remove the current user from the list of people they can message
    if user in available_users:
        available_users.remove(user)

    context = {
        'segment': 'communication',
        'contacts': contacts,
        'project_boards': project_boards,
        'available_users': available_users
    }
    return render(request, 'apps/communication/chat_dashboard.html', context)


@login_required
def chat_room(request, user_id):
    """
    Displays the conversation history with a specific user and
    processes new messages.
    """
    other_user = get_object_or_404(User, pk=user_id)
    user = request.user

    # Automatically mark all incoming messages from this user as read
    Message.objects.filter(
        sender=other_user,
        recipient=user,
        is_read=False
    ).update(is_read=True)

    # Handle sending a new message
    if request.method == 'POST':
        body = request.POST.get('body')
        if body:
            Message.objects.create(
                sender=user,
                recipient=other_user,
                subject="Chat Message",  # Default subject for simple chat
                body=body
            )
            # Redirect back to the same page to prevent double-submission on
            # refresh
            return redirect('communication:chat_room', user_id=user_id)

    # Retrieve the full message history between these two users
    messages_history = Message.objects.filter(
        Q(sender=user, recipient=other_user) | Q(sender=other_user, recipient=user)
    ).order_by('timestamp')

    context = {
        'segment': 'communication',
        'other_user': other_user,
        'messages_history': messages_history
    }
    return render(request, 'apps/communication/chat_room.html', context)


from ..project.decorators import project_membership_required

@login_required
@project_membership_required
def project_board(request, project_id):
    """
    Displays the discussion board for a specific project.
    Allows members to post updates and questions.
    """
    project = get_object_or_404(Project, pk=project_id)

    # Update the user's access log for this board to mark current posts as
    # 'read'
    ProjectBoardAccess.objects.update_or_create(
        user=request.user,
        project=project,
        defaults={'last_accessed': timezone.now()}
    )

    # Handle a new post submission
    if request.method == 'POST':
        form = ProjectPostForm(request.POST)
        if form.is_valid():
            post = form.save(commit=False)
            post.project = project
            post.author = request.user
            post.save()

            # Update access log again so the user's own new post isn't
            # immediately flagged as 'new' for them.
            ProjectBoardAccess.objects.update_or_create(
                user=request.user,
                project=project,
                defaults={'last_accessed': timezone.now()}
            )
            return redirect(
                'communication:project_board',
                project_id=project.id)
    else:
        # Provide a blank form for GET requests
        form = ProjectPostForm()

    # Fetch all posts belonging to this project
    posts = project.posts.all()

    context = {
        'segment': 'project',
        'project': project,
        'posts': posts,
        'form': form
    }
    return render(request, 'apps/communication/project_board.html', context)
