"""
Admin configuration for the network app.
This file registers the models with the Django admin interface,
allowing administrators to manage Swarm Networks and Participants.
"""

from django.contrib import admin
from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork


@admin.register(SwarmNetwork)
class SwarmNetworkAdmin(admin.ModelAdmin):
    """
    Admin interface for SwarmNetwork model.
    """
    list_display = ('name', 'project', 'status', 'author', 'created_at')
    list_filter = ('status', 'project')
    search_fields = ('name', 'description')
    readonly_fields = ('identifier', 'created_at', 'updated_at')


@admin.register(SwarmParticipant)
class SwarmParticipantAdmin(admin.ModelAdmin):
    """
    Admin interface for SwarmParticipant model.
    """
    list_display = ('user', 'network', 'role', 'participant_id')
    list_filter = ('role', 'network')
    search_fields = ('participant_id', 'user__username')


@admin.register(UserCurrentNetwork)
class UserCurrentNetworkAdmin(admin.ModelAdmin):
    """
    Admin interface for tracking users' current active network.
    """
    list_display = ('user', 'network')
    search_fields = ('user__username', 'network__name')
