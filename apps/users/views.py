"""
View functions for the users application.
Handles authentication (sign in, sign up, sign out), password management,
profile updates, and administrative user management.
"""

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.contrib.auth.views import (
    LoginView,
    PasswordChangeView,
    PasswordResetConfirmView,
    PasswordResetView
)
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView

from apps.users.forms import (
    ProfileForm,
    SigninForm,
    SignupForm,
    UserPasswordChangeForm,
    UserPasswordResetForm,
    UserSetPasswordForm,
    UserUpdateForm
)
from apps.users.models import Profile
from apps.users.utils import user_filter

from .decorators import admin_required


def index(request):
    """Simple index view for users (mainly for testing)."""
    return HttpResponse("INDEX Users")


class SignInView(LoginView):
    """Standard Django LoginView customized with our SigninForm and template."""
    form_class = SigninForm
    template_name = "authentication/sign-in.html"


class SignUpView(CreateView):
    """Standard Django CreateView for user registration."""
    form_class = SignupForm
    template_name = "authentication/sign-up.html"
    success_url = reverse_lazy('users:signin')


class UserPasswordChangeView(PasswordChangeView):
    """View to allow users to change their password while logged in."""
    template_name = 'authentication/password-change.html'
    form_class = UserPasswordChangeForm


class UserPasswordResetView(PasswordResetView):
    """View to initiate the password reset process via email."""
    template_name = 'authentication/forgot-password.html'
    form_class = UserPasswordResetForm


class UserPasswrodResetConfirmView(PasswordResetConfirmView):
    """View to finalize password reset after clicking the email link."""
    template_name = 'authentication/reset-password.html'
    form_class = UserSetPasswordForm


def signout_view(request):
    """Logs out the current user and redirects to the sign-in page."""
    logout(request)
    return redirect(reverse('users:signin'))


@login_required(login_url='/users/signin/')
def profile(request):
    """Displays and handles updates for the logged-in user's profile."""
    # Retrieve or create the Profile associated with the current user.
    user_profile, created = Profile.objects.get_or_create(
        user=request.user,
        defaults={'role': 'admin' if request.user.is_superuser else 'user'}
    )

    if request.method == 'POST':
        # If the form was submitted, process the POST data.
        form = ProfileForm(request.POST, instance=user_profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully')
    else:
        # If it's a GET request, pre-populate the form with current data.
        form = ProfileForm(instance=user_profile)

    context = {
        'form': form,
        'segment': 'profile',
    }
    return render(request, 'dashboard/profile.html', context)


@login_required(login_url='/users/signin/')
def change_password(request):
    """
    Handles a password change request using a simple POST method.
    Verifies the current password before setting the new one.
    """
    user = request.user
    if request.method == 'POST':
        current_pwd = request.POST.get('current_password')
        new_pwd = request.POST.get('new_password')

        # Verify the user knows their current password.
        if check_password(current_pwd, user.password):
            user.set_password(new_pwd)
            user.save()
            messages.success(request, 'Password changed successfully')
        else:
            messages.error(request, "Current password doesn't match!")

    # Redirect back to the page the user came from.
    return redirect(request.META.get('HTTP_REFERER', '/'))


@admin_required
def user_list(request):
    """
    Administrative view to list, search, and manage all users.
    Includes pagination and user creation capabilities.
    """
    # Generate database filters based on search queries in the GET parameters.
    filters = user_filter(request)
    users_queryset = User.objects.filter(**filters).order_by('username')

    # Empty form for creating new users.
    form = SignupForm()

    # Set up pagination (5 users per page).
    page_number = request.GET.get('page', 1)
    paginator = Paginator(users_queryset, 5)
    users_page = paginator.get_page(page_number)

    if request.method == 'POST':
        # Handle new user creation from the admin panel.
        form = SignupForm(request.POST)
        if form.is_valid():
            new_user = form.save()
            # Manually set the role from the form's cleaned data.
            new_user.profile.role = form.cleaned_data['role']
            new_user.profile.save()
            return redirect(request.META.get('HTTP_REFERER', '/'))

    context = {
        'segment': 'users',
        'users': users_page,
        'form': form,
    }
    return render(request, 'apps/users.html', context)


@login_required(login_url='/users/signin/')
def delete_user(request, id):
    """Deletes a user by their ID. Protected by login requirement."""
    # Note: In a production environment, this should likely be admin_required.
    user_to_delete = get_object_or_404(User, id=id)
    user_to_delete.delete()
    return redirect(request.META.get('HTTP_REFERER', '/'))


@login_required(login_url='/users/signin/')
def update_user(request, id):
    """Updates a user's details (username, email, role) from the admin panel."""
    user_to_update = get_object_or_404(User, id=id)

    if request.method == 'POST':
        form = UserUpdateForm(request.POST, instance=user_to_update)
        if form.is_valid():
            updated_user = form.save()
            # Update the profile role as well.
            updated_user.profile.role = form.cleaned_data['role']
            updated_user.profile.save()
            messages.success(request, 'User updated successfully')

    return redirect(request.META.get('HTTP_REFERER', '/'))


@login_required(login_url='/users/signin/')
def user_change_password(request, id):
    """Allows an administrator to forcefully reset a user's password."""
    user_to_change = get_object_or_404(User, id=id)

    if request.method == 'POST':
        new_password = request.POST.get('password')
        if new_password:
            user_to_change.set_password(new_password)
            user_to_change.save()
            messages.success(
                request, f'Password updated for {user_to_change.username}')

    return redirect(request.META.get('HTTP_REFERER', '/'))
