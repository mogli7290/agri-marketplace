"""Authentication backends."""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

UserModel = get_user_model()


class EmailOrUsernameBackend(ModelBackend):
    """Authenticate against username *or* email, case-insensitively."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = username or kwargs.get("email")
        if identifier is None or password is None:
            return None
        try:
            user = UserModel.objects.get(
                Q(username__iexact=identifier) | Q(email__iexact=identifier)
            )
        except UserModel.DoesNotExist:
            # Run the default hasher anyway to mitigate timing attacks.
            UserModel().set_password(password)
            return None
        except UserModel.MultipleObjectsReturned:
            user = (
                UserModel.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier))
                .order_by("id")
                .first()
            )
        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
