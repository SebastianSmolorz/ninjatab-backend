<wizard-report>
# PostHog post-wizard report

The wizard has completed a deep integration of PostHog analytics into the NinjaTab Django backend. The following changes were made:

- **`ninjatab/settings/base.py`** — Added `POSTHOG_PROJECT_TOKEN`, `POSTHOG_HOST`, and `POSTHOG_DISABLED` settings read from environment variables. Added `posthog.integrations.django.PosthogContextMiddleware` to `MIDDLEWARE` for automatic request context, tracing header extraction, and exception autocapture.
- **`ninjatab/tabs/apps.py`** — Initialized the PostHog SDK in `TabsConfig.ready()` using settings values. Registered `posthog.shutdown` with `atexit` to ensure all events flush on exit.
- **`ninjatab/auth/api.py`** — Added `user_signed_up` and `user_logged_in` events on magic link verification, and `social_login_completed` on social login. Users are identified with `identify_context()` at each auth event.
- **`ninjatab/tabs/api.py`** — Added `tab_created`, `tab_settled`, `tab_simplified`, `settlement_marked_paid`, `bill_created`, `bill_splits_submitted`, `receipt_scanned`, and `invite_claimed` events.
- **`ninjatab/marketing/api.py`** — Added `waitlist_joined` event.
- **`requirements.txt`** — Added `posthog` dependency.
- **`.env`** — Added `POSTHOG_PROJECT_TOKEN` and `POSTHOG_HOST` environment variables.

| Event | Description | File |
|-------|-------------|------|
| `user_signed_up` | New user registered via magic link | `ninjatab/auth/api.py` |
| `user_logged_in` | Existing user authenticated via magic link | `ninjatab/auth/api.py` |
| `social_login_completed` | User authenticated via Google or Apple | `ninjatab/auth/api.py` |
| `tab_created` | A new shared expense tab was created | `ninjatab/tabs/api.py` |
| `tab_settled` | A tab was settled (closed) | `ninjatab/tabs/api.py` |
| `tab_simplified` | Settlements were calculated for a tab | `ninjatab/tabs/api.py` |
| `settlement_marked_paid` | A settlement payment was marked as paid | `ninjatab/tabs/api.py` |
| `bill_created` | A new bill was added to a tab | `ninjatab/tabs/api.py` |
| `bill_splits_submitted` | Split allocations were submitted for a bill | `ninjatab/tabs/api.py` |
| `receipt_scanned` | A receipt was uploaded and OCR-scanned | `ninjatab/tabs/api.py` |
| `invite_claimed` | A user claimed a placeholder via invite link | `ninjatab/tabs/api.py` |
| `waitlist_joined` | A new user joined the marketing waitlist | `ninjatab/marketing/api.py` |

## Next steps

We've built some insights and a dashboard for you to keep an eye on user behavior, based on the events we just instrumented:

- **Dashboard**: [Analytics basics](https://eu.posthog.com/project/159969/dashboard/624220)
- **Insight**: [New user signups over time](https://eu.posthog.com/project/159969/insights/9t59woqu) — Daily signups via magic link and social login
- **Insight**: [User activation funnel](https://eu.posthog.com/project/159969/insights/LBeeSeo2) — Conversion: signed up → tab created → bill added → tab settled
- **Insight**: [Bills and tabs created over time](https://eu.posthog.com/project/159969/insights/VyG0Bj9r) — Weekly engagement volume
- **Insight**: [Receipt scan and invite adoption](https://eu.posthog.com/project/159969/insights/X7k84WRW) — Premium feature usage
- **Insight**: [Login method breakdown](https://eu.posthog.com/project/159969/insights/Ol3JgyBX) — Social login by provider (Google vs Apple)

### Agent skill

We've left an agent skill folder in your project. You can use this context for further agent development when using Claude Code. This will help ensure the model provides the most up-to-date approaches for integrating PostHog.

</wizard-report>
