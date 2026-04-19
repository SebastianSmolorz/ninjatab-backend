from posthog.integrations.django import PosthogContextMiddleware


class AnonymousAwarePosthogMiddleware(PosthogContextMiddleware):
    def _resolve_user_details(self, user):
        user_id, email = super()._resolve_user_details(user)
        if user_id is None:
            user_id = "anonymous"
        return user_id, email
