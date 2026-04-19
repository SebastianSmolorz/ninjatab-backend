from posthog.integrations.django import PosthogContextMiddleware


class AnonymousAwarePosthogMiddleware(PosthogContextMiddleware):
    def _resolve_user_details(self, user):
        _, email = super()._resolve_user_details(user)
        user_id = None

        is_authenticated = getattr(user, "is_authenticated", False)
        if callable(is_authenticated):
            is_authenticated = is_authenticated()

        if is_authenticated:
            uuid = getattr(user, "uuid", None)
            user_id = str(uuid) if uuid is not None else None

        if user_id is None:
            user_id = "anonymous"

        return user_id, email
