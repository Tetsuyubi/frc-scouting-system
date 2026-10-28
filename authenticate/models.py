from django.db import models

class AuthorizedUser(models.Model):
    ROLE_CHOICES = [
        ('admin',      'Administrator'),
        ('student',    'Student'),
        ('scout_tech', 'Scout Tech'),
        ('strategist', 'Strategist'),
        ('teacher',    'Teacher'),
    ]
    VALID_ROLES = {v for v, _ in ROLE_CHOICES}

    email = models.EmailField(unique=True)
    roles = models.CharField(max_length=200, default='student')
    created_at = models.DateTimeField(auto_now_add=True)

    def get_roles_list(self):
        return [
            role for role in (r.strip() for r in self.roles.split(','))
            if role in self.VALID_ROLES
        ]

    @property
    def is_admin(self):
        return 'admin' in self.get_roles_list()

    def __str__(self):
        return self.email


class SiteSettings(models.Model):
    """Singleton-like site-wide settings stored in the DB.

    Holds configurable branding and theme values to avoid local file storage.
    """
    theme_color = models.CharField(max_length=7, default="#e74c3c")
    logo_url = models.CharField(max_length=512, default="/static/images/logo.png")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"SiteSettings(color={self.theme_color})"