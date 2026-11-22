# site-feedback-notifier

Hourly framework agent: emails the fleet operator new reader feedback stored by
each subscribed site (the FeedbackWidget's `/api/feedback` rows). One instance
serves all sites; sites subscribe in the storage copy of
`config/site-feedback-notifier-config.json` (`by_site`). Read-only on site
databases; never calls an LLM. Runbook: [AGENT.md](AGENT.md). Goals:
[goals.json](goals.json). Tests: `framework/tests/test_site_feedback_notifier.py`.
