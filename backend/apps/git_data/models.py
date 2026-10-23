from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone


class AccountType(models.TextChoices):
    BOT = 'Bot', 'Bot'
    ORGANIZATION = 'Organization', 'Organization'
    USER = 'User', 'User'


class DiscoveryMethod(models.TextChoices):
    COLLABORATOR = 'Collaborator', 'Collaborator'
    CONTRIBUTOR = 'Contributor', 'Contributor'
    FOLLOWER = 'Follower', 'Follower'
    FOLLOWING = 'Following', 'Following'
    ORG_MEMBER = 'OrgMember', 'OrgMember'
    SEARCH = 'Search', 'Search'


class RelationshipType(models.TextChoices):
    COLLABORATOR = 'Collaborator', 'Collaborator'
    CONTRIBUTOR = 'Contributor', 'Contributor'
    FOLLOWING = 'Following', 'Following'
    FOLLOWER = 'Follower', 'Follower'
    ORG_MEMBER = 'OrgMember', 'OrgMember'


class UserStatus(models.TextChoices):
    UNKNOWN = 'Unknown', 'Unknown'
    CONFIRMED = 'Confirmed', 'Confirmed'
    HIDDEN = 'Hidden', 'Hidden'


class Tag(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'git_tag'
        ordering = ['name']

    def __str__(self):
        return self.name


class User(models.Model):
    username = models.CharField(max_length=255, unique=True)
    account_type = models.CharField(
        max_length=20,
        choices=AccountType.choices,
        default='User'
    )
    discovery_method = models.CharField(
        max_length=20,
        choices=DiscoveryMethod.choices,
        default='Search'
    )
    status = models.CharField(
        max_length=20,
        choices=UserStatus.choices,
        default='Unknown'
    )

    name = models.CharField(max_length=255, null=True, blank=True)
    email = models.CharField(max_length=4096, null=True, blank=True)
    avatar = models.URLField(null=True, blank=True)
    bio = models.TextField(null=True, blank=True)
    company = models.CharField(max_length=4096, null=True, blank=True)
    location = models.CharField(max_length=4096, null=True, blank=True)
    url = models.URLField()

    processed_at = models.DateTimeField(null=True, blank=True)
    scanned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    source_created_at = models.DateTimeField(null=True, blank=True)

    source_user_id = models.BigIntegerField()
    tags = models.ManyToManyField('Tag', blank=True, related_name='users')

    class Meta:
        db_table = 'git_user'
        indexes = [
            models.Index(fields=['username']),
            models.Index(fields=['status']),
            models.Index(fields=['account_type']),
            models.Index(fields=['processed_at']),
            models.Index(fields=['scanned_at']),
        ]

    def __str__(self):
        return f"{self.username}"


class Repo(models.Model):
    name = models.CharField(max_length=255)
    full_name = models.CharField(max_length=511)

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='repos'
    )

    source_repo_id = models.BigIntegerField()
    description = models.TextField(null=True, blank=True)
    default_branch = models.CharField(max_length=255)
    url = models.URLField()
    stars = models.IntegerField(default=0)
    size = models.IntegerField(default=0)
    is_fork = models.BooleanField(default=False)
    homepage = models.URLField(null=True, blank=True)
    languages = ArrayField(models.CharField(
        max_length=100), default=list, blank=True)

    source_created_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)

    tags = ArrayField(models.CharField(max_length=100),
                      default=list, blank=True)
    latest_event_checked = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'git_repo'
        indexes = [
            models.Index(fields=['full_name']),
            models.Index(fields=['source_repo_id']),
            models.Index(fields=['owner']),
            models.Index(fields=['stars']),
            models.Index(fields=['processed_at']),
        ]

    def __str__(self):
        return self.full_name


class Commit(models.Model):
    sha = models.CharField(max_length=40)

    repo = models.ForeignKey(
        Repo,
        on_delete=models.CASCADE,
        related_name='commits'
    )

    author = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='authored_commits',
        null=True,
        blank=True
    )

    committer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='committed_commits',
        null=True,
        blank=True
    )

    message = models.TextField()
    url = models.URLField()
    commit_date = models.DateTimeField(null=True, blank=True)
    branch_name = models.CharField(max_length=255)
    pr_number = models.IntegerField(null=True, blank=True)
    from_event = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    additions = models.IntegerField(default=0)
    deletions = models.IntegerField(default=0)

    class Meta:
        db_table = 'git_commit'
        indexes = [
            models.Index(fields=['sha']),
            models.Index(fields=['repo', 'author']),
            models.Index(fields=['commit_date']),
            models.Index(fields=['branch_name']),
            models.Index(fields=['processed_at']),
        ]
        unique_together = [['sha', 'repo']]

    def __str__(self):
        return f"{self.sha[:8]} - {self.message[:50]}"


class Gist(models.Model):
    gist_id = models.CharField(max_length=255)
    revision_id = models.CharField(max_length=255)

    author = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='gists'
    )

    url = models.URLField()
    description = models.TextField(null=True, blank=True)
    filenames = ArrayField(models.CharField(
        max_length=4096), default=list, blank=True)
    is_fork = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'git_gist'
        indexes = [
            models.Index(fields=['author']),
            models.Index(fields=['source_created_at']),
            models.Index(fields=['gist_id', 'revision_id']),
            models.Index(fields=['processed_at']),
        ]

        unique_together = [['gist_id', 'revision_id']]

    def __str__(self):
        return f"Gist {self.gist_id} (rev {self.revision_id}) by {self.author.username}"


class UserRelationship(models.Model):
    """User relationship model representing relationships between users"""

    from_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='outgoing_relationships'
    )

    to_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='incoming_relationships'
    )

    repo = models.ForeignKey(
        Repo,
        on_delete=models.CASCADE,
        related_name='relationships',
        null=True,
        blank=True
    )

    relationship_type = models.CharField(
        max_length=20,
        choices=RelationshipType.choices
    )

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'git_user_relationship'
        indexes = [
            models.Index(fields=['from_user', 'relationship_type']),
            models.Index(fields=['to_user', 'relationship_type']),
            models.Index(fields=['repo']),
        ]
        unique_together = [
            ['from_user', 'to_user', 'relationship_type', 'repo']]

    def __str__(self):
        repo_info = f" (via {self.repo.name})" if self.repo else ""
        return f"{self.from_user.username} -> {self.to_user.username} ({self.relationship_type}){repo_info}"
