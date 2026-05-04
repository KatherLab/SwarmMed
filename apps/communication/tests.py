from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from communication.models import ProjectBoardAccess, ProjectPost
from project.models import Project


class ChatDashboardPerformanceTests(TestCase):
    def setUp(self):
        # Bandit B106: hardcoded password is test-only.
        self.user = User.objects.create_user(
            username="testuser", password="password"
        )  # nosec B106
        # ``LegalAcceptanceMiddleware`` redirects every authenticated user who
        # has not yet accepted the terms / privacy policy; without these flags
        # the dashboard returns 302 and the assertions below never run.
        self.user.profile.accepted_terms = True
        self.user.profile.accepted_policy = True
        self.user.profile.save(
            update_fields=["accepted_terms", "accepted_policy"]
        )
        self.client = Client()
        self.client.force_login(self.user)

        # Create projects
        self.project1 = Project.objects.create(
            title="Project 1",
            author=self.user,
            identifier="11111111-1111-1111-1111-111111111111",
        )
        self.project2 = Project.objects.create(
            title="Project 2",
            author=self.user,
            identifier="22222222-2222-2222-2222-222222222222",
        )

        # Create posts for Project 1 (User visited in the past)
        # Create a past access log
        past_time = timezone.now() - timedelta(days=1)
        access = ProjectBoardAccess.objects.create(
            user=self.user, project=self.project1
        )
        # Use update() to force a past timestamp on an auto_now field
        ProjectBoardAccess.objects.filter(id=access.id).update(
            updated_at=past_time
        )

        # Old post (read)
        ProjectPost.objects.create(
            project=self.project1,
            author=self.user,
            content="Old post",
        )
        # Hack to set timestamp in past (auto_now_add makes it hard)
        # We'll just rely on the fact that the access log was created with 'past_time'
        # BUT auto_now_add uses time of creation.
        # To test 'unread', we need posts created AFTER the access log.
        # Since access log was set to yesterday, any post created NOW is unread.

        ProjectPost.objects.create(
            project=self.project1,
            author=self.user,
            content="New unread post 1",
        )
        ProjectPost.objects.create(
            project=self.project1,
            author=self.user,
            content="New unread post 2",
        )

        # Create posts for Project 2 (User NEVER visited)
        ProjectPost.objects.create(
            project=self.project2, author=self.user, content="Unread post P2"
        )

    def test_chat_dashboard_unread_counts(self):
        response = self.client.get(reverse("communication:chat_dashboard"))
        self.assertEqual(response.status_code, 200)

        boards = response.context["project_boards"]
        board1 = next(b for b in boards if b["project"].id == self.project1.id)
        board2 = next(b for b in boards if b["project"].id == self.project2.id)

        # Project 1: 3 posts total, but access log was yesterday.
        # Wait, auto_now_add uses current time.
        # So ALL posts are created NOW.
        # The access log was set to YESTERDAY.
        # So ALL posts are newer than access log.
        # So count should be 3.

        # Let's adjust logic.
        # To test "read" posts, we need an access log NEWER than the post.
        # But we can't easily change post timestamp if it's auto_now_add=True without update()

        # Update one post to be very old
        old_post = ProjectPost.objects.filter(project=self.project1).first()
        old_post.created_at = timezone.now() - timedelta(days=2)
        old_post.save()

        # Now:
        # P1 has 1 old post (2 days ago)
        # Access log is 1 day ago.
        # P1 has 2 new posts (now).
        # Expected unread: 2.

        # Re-fetch because we modified data
        response = self.client.get(reverse("communication:chat_dashboard"))
        boards = response.context["project_boards"]
        board1 = next(b for b in boards if b["project"].id == self.project1.id)
        board2 = next(b for b in boards if b["project"].id == self.project2.id)

        self.assertEqual(board1["unread_count"], 2)

        # Project 2: Never visited. All posts (1) are unread.
        self.assertEqual(board2["unread_count"], 1)

    def test_performance_query_count(self):
        # Initial warmup
        self.client.get(reverse("communication:chat_dashboard"))

        # The dashboard must stay ``O(1)`` w.r.t. number of projects. The
        # exact count drifts as Django and the view are optimised; we assert
        # an upper bound that also holds when extra projects are present.
        # Today: 6 queries empty, 8 with projects+posts populated. Anything
        # higher than ``MAX_QUERIES`` would indicate an N+1 regression.
        MAX_QUERIES = 8

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("communication:chat_dashboard"))
        self.assertLessEqual(
            len(ctx.captured_queries),
            MAX_QUERIES,
            f"Empty-state dashboard ran {len(ctx.captured_queries)} queries (>{MAX_QUERIES})",
        )

        # Create MORE projects and ensure query count doesn't jump per project.
        for i in range(5):
            p = Project.objects.create(title=f"Extra {i}", author=self.user)
            ProjectPost.objects.create(
                project=p, author=self.user, content="Content"
            )

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("communication:chat_dashboard"))
        self.assertLessEqual(
            len(ctx.captured_queries),
            MAX_QUERIES,
            f"Populated dashboard ran {len(ctx.captured_queries)} queries (>{MAX_QUERIES})",
        )
