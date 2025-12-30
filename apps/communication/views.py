from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Q, Max, Count
from .models import Message, ProjectPost, ProjectBoardAccess
from .forms import MessageForm, ProjectPostForm
from apps.project.models import Project, UserCurrentProject
from django.utils import timezone

@login_required(login_url='/users/signin/')
def chat_dashboard(request):
    """
    Lists all users the current user has exchanged messages with, 
    ordered by the most recent message timestamp.
    Also lists project boards.
    """
    user = request.user
    
    # --- 1. Direct Messages Logic ---
    sent_to = Message.objects.filter(sender=user).values_list('recipient', flat=True)
    received_from = Message.objects.filter(recipient=user).values_list('sender', flat=True)
    contact_ids = set(list(sent_to) + list(received_from))
    
    contacts = []
    for contact_id in contact_ids:
        contact = User.objects.get(id=contact_id)
        
        last_msg = Message.objects.filter(
            Q(sender=user, recipient=contact) | Q(sender=contact, recipient=user)
        ).order_by('-timestamp').first()
        
        unread_count = Message.objects.filter(
            sender=contact, recipient=user, is_read=False
        ).count()
        
        contacts.append({
            'user': contact,
            'last_message': last_msg,
            'unread_count': unread_count
        })
    
    contacts.sort(key=lambda x: x['last_message'].timestamp if x['last_message'] else None, reverse=True)
    
    # --- 2. Project Boards Logic ---
    user_projects = Project.objects.filter(Q(author=user) | Q(members=user)).distinct()
    
    project_boards = []
    for project in user_projects:
        # Get unread count
        try:
            access_log = ProjectBoardAccess.objects.get(user=user, project=project)
            last_accessed = access_log.last_accessed
        except ProjectBoardAccess.DoesNotExist:
            last_accessed = None
        
        if last_accessed:
            unread_posts = ProjectPost.objects.filter(project=project, timestamp__gt=last_accessed).count()
        else:
            unread_posts = ProjectPost.objects.filter(project=project).count()
            
        last_post = ProjectPost.objects.filter(project=project).order_by('-timestamp').first()
        
        project_boards.append({
            'project': project,
            'unread_count': unread_posts,
            'last_post': last_post
        })
        
    # --- 3. Available Users for New Chat ---
    available_users = set()
    for p in user_projects:
        available_users.add(p.author)
        for m in p.members.all():
            available_users.add(m)
    if user in available_users:
        available_users.remove(user)

    context = {
        'segment': 'communication',
        'contacts': contacts,
        'project_boards': project_boards,
        'available_users': available_users
    }
    return render(request, 'apps/communication/chat_dashboard.html', context)

@login_required(login_url='/users/signin/')
def chat_room(request, user_id):
    """
    Displays the conversation with a specific user and allows sending messages.
    """
    other_user = get_object_or_404(User, pk=user_id)
    user = request.user
    
    # Mark all messages from other_user as read
    Message.objects.filter(sender=other_user, recipient=user, is_read=False).update(is_read=True)
    
    if request.method == 'POST':
        body = request.POST.get('body')
        if body:
            Message.objects.create(
                sender=user,
                recipient=other_user,
                subject="Chat Message", 
                body=body
            )
            return redirect('communication:chat_room', user_id=user_id)
    
    messages_history = Message.objects.filter(
        Q(sender=user, recipient=other_user) | Q(sender=other_user, recipient=user)
    ).order_by('timestamp')
    
    context = {
        'segment': 'communication',
        'other_user': other_user,
        'messages_history': messages_history
    }
    return render(request, 'apps/communication/chat_room.html', context)

@login_required(login_url='/users/signin/')
def project_board(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    
    # Permission check
    if request.user != project.author and request.user not in project.members.all():
        messages.error(request, "You do not have permission to view this project's board.")
        return redirect('project_list')

    # Update Access Log
    ProjectBoardAccess.objects.update_or_create(
        user=request.user,
        project=project,
        defaults={'last_accessed': timezone.now()}
    )

    if request.method == 'POST':
        form = ProjectPostForm(request.POST)
        if form.is_valid():
            post = form.save(commit=False)
            post.project = project
            post.author = request.user
            post.save()
            # Update access log again so the user's own post doesn't count as unread immediately (optional, but good UX)
            ProjectBoardAccess.objects.update_or_create(
                user=request.user,
                project=project,
                defaults={'last_accessed': timezone.now()}
            )
            return redirect('communication:project_board', project_id=project.id)
    else:
        form = ProjectPostForm()
    
    posts = project.posts.all()
    
    context = {
        'segment': 'project', 
        'project': project,
        'posts': posts,
        'form': form
    }
    return render(request, 'apps/communication/project_board.html', context)
