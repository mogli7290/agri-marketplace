"""Custom DRF permissions."""

from rest_framework import permissions


class IsFarmer(permissions.BasePermission):
    message = "Only farmer/FPO accounts can perform this action."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and hasattr(request.user, "farmer_profile"))


class IsBuyer(permissions.BasePermission):
    message = "Only buyer accounts can perform this action."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and hasattr(request.user, "buyer_profile"))


class IsOwnerOrReadOnly(permissions.BasePermission):
    """Object access limited to its owner (via ``owner_field``) or staff."""

    owner_field = "user"

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS or request.user.is_staff:
            return True
        owner_field = getattr(view, "owner_field", self.owner_field)
        if owner_field == "farmer":
            return getattr(obj, "farmer", None) is not None and obj.farmer.user_id == request.user.id
        owner = getattr(obj, owner_field, None)
        return owner == request.user
