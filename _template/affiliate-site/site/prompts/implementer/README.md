# Implementer runbook addenda

`prompts/implementer/<dispatch-kind>.md` (for example `article-author.md`) is
appended to the reusable-agents runbook for that dispatch kind when the
implementer works on this site. Use it for what this site does differently:
where hero images come from (the local image daemon on :7861, or none; never
an Amazon image), the byline (site.json), that articles are INSERTed as
drafts for the publish gate, which views picks come from, and the claims the
niche never makes.

These files are operator-owned: the implementer scope excludes `prompts/**`
for every dispatch kind, where the framework applies the scope (not yet on
the implementer's claude path: `agents/README.md`, "Prerequisite").
