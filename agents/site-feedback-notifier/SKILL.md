# site-feedback-notifier — skill card

Use when the operator asks "did anyone send feedback?", "why didn't I get the
feedback email?", or wants a site's feedback widget reported by email.

- Subscribe a site: add `by_site.<site>` to the storage copy of
  `config/site-feedback-notifier-config.json` (see AGENT.md).
- Check now without sending: `FEEDBACK_NOTIFIER_DRY_RUN=1 python3 agents/site-feedback-notifier/agent.py`
  (secrets.env loaded, `STORAGE_BACKEND=azure`).
- Read the feedback itself: the site's admin view (`admin_url` in the config).
- Never post test submissions to a production site to "check" it.
