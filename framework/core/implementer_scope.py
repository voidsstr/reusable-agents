"""Path-scope policy for the implementer.

A small, dep-free primitive that takes the optional `implementer` block
from a site.yaml and decides, given a target file path, whether the
implementer is allowed to modify it. Used by:

  * build-aider-invocation.py — to filter recs whose target_files
    violate the policy (those recs get marked "out-of-scope" + deferred
    instead of dispatched to the LLM).
  * run.sh — to refuse a commit that, after aider finished, ended up
    touching files outside the policy.
  * the implementer's post-apply hooks — to decide whether to kick the
    mobile build / backend deploy based on whether their respective
    paths were touched.

Why a separate module instead of inlining in build-aider-invocation:
the same policy is consulted at three distinct lifecycle points, and
the framework gains a guardrail other agents (digest, h2h, etc.) can
adopt with one import.

Glob syntax: fnmatch — `mobile/**` matches `mobile/anything`,
`mobile/sub/file.ts`, etc.  POSIX bracket / star semantics, no regex.

Example site.yaml block:

    implementer:
      allowed_paths:
        - "src/**"
        - "frontend/**"
        - "db/migrations/**"
        - "scripts/**"
        - "*.md"
        - "blog/**"
      excluded_paths:
        - "mobile/**"
        - "ios-extensions/**"
      post_apply:
        kick_mobile_build: false
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ScopePolicy:
    """Resolved policy decision surface."""

    allowed_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    # post-apply hooks default to True for backward compat — legacy
    # configs without the post_apply block keep their old behaviour.
    kick_mobile_build: bool = True
    kick_backend_deploy: bool = True

    @classmethod
    def from_site_config(
        cls,
        site_cfg: dict | None,
        *,
        dispatch_kind: str | None = None,
    ) -> "ScopePolicy":
        """Build a policy from a site.yaml dict.

        When `dispatch_kind` is set and the config has a matching entry
        under `implementer.scope_by_dispatch_kind.<kind>`, the per-kind
        block REPLACES (not merges with) the default policy. Rationale:
        the crash-watcher needs to edit mobile/ while SEO must not, so
        their scope rules are mutually exclusive — merging would let
        SEO drift into mobile by accident. Authors can copy the default
        keys into the per-kind block if they want partial overlap.

        Example site.yaml:

            implementer:
              allowed_paths: ["src/**", "frontend/**"]
              excluded_paths: ["mobile/**"]
              scope_by_dispatch_kind:
                crash-fix:
                  allowed_paths: ["src/**", "frontend/**", "mobile/**"]
                  excluded_paths: []
                  post_apply:
                    kick_mobile_build: true

        `dispatch_kind="crash-fix"` here yields a policy that allows
        mobile/ and will kick the EAS build on apply. Any other
        dispatch_kind (or no dispatch_kind) falls back to the default.
        """
        impl = (site_cfg or {}).get("implementer") or {}
        if not isinstance(impl, dict):
            return cls()

        # Choose which block defines this dispatch's scope.
        block: dict = impl
        per_kind = impl.get("scope_by_dispatch_kind")
        if dispatch_kind and isinstance(per_kind, dict):
            override = per_kind.get(dispatch_kind)
            if isinstance(override, dict):
                block = override

        allowed = tuple(_normalize(p) for p in (block.get("allowed_paths") or []) if isinstance(p, str))
        excluded = tuple(_normalize(p) for p in (block.get("excluded_paths") or []) if isinstance(p, str))
        post = block.get("post_apply") or {}
        return cls(
            allowed_paths=allowed,
            excluded_paths=excluded,
            kick_mobile_build=bool(post.get("kick_mobile_build", True)),
            kick_backend_deploy=bool(post.get("kick_backend_deploy", True)),
        )

    def is_path_allowed(self, path: str) -> bool:
        """Return True if the implementer may write to `path`.

        Logic, in order:
          1. If `excluded_paths` matches → DENY (denylist always wins).
          2. If `allowed_paths` is non-empty → must match → else DENY.
          3. No policy set → ALLOW (legacy behaviour).
        """
        p = _normalize(path)
        for pat in self.excluded_paths:
            if _match(p, pat):
                return False
        if self.allowed_paths:
            for pat in self.allowed_paths:
                if _match(p, pat):
                    return True
            return False
        return True

    def filter_files(self, paths: Iterable[str]) -> tuple[list[str], list[str]]:
        """Split a list of paths into (allowed, denied)."""
        allowed: list[str] = []
        denied: list[str] = []
        for p in paths:
            (allowed if self.is_path_allowed(p) else denied).append(p)
        return allowed, denied

    def is_rec_in_scope(self, rec_files: Sequence[str]) -> tuple[bool, list[str]]:
        """Apply the policy to a rec's target file list.

        Returns (ok, denied_paths). ok=False means the rec should be
        deferred with reason "out-of-scope per implementer.{allowed,
        excluded}_paths". If `rec_files` is empty the rec is permitted
        — the implementer will fall back to its repo-wide aider scan,
        and any touched files get policed at write-time via
        `is_path_allowed()` again.
        """
        if not rec_files:
            return True, []
        _, denied = self.filter_files(rec_files)
        return (not denied, denied)

    def touched_mobile(self, paths: Iterable[str]) -> bool:
        """Heuristic: should the post-apply hook trigger a mobile build?

        Returns True iff any path looks like a mobile artifact, regardless
        of whether the policy actually permitted it (the hook is asking
        "did this commit need a mobile build?", not "should it have been
        allowed in?").
        """
        return any(_match(_normalize(p), pat) for p in paths for pat in MOBILE_PATTERNS)

    def touched_backend(self, paths: Iterable[str]) -> bool:
        """Same idea for backend deploy decisions."""
        return any(_match(_normalize(p), pat) for p in paths for pat in BACKEND_PATTERNS)


# What "mobile" looks like across the framework's supported app shapes.
# Keep this list short + well-known; sites with weirder layouts can use
# excluded_paths to be explicit.
MOBILE_PATTERNS: tuple[str, ...] = (
    "mobile/**",
    "ios-extensions/**",
    "android/**",
    "ios/**",
)

# Backend = anything that lands behind the Container App / API. Mirrors
# the deploy gate in CLAUDE.md.
BACKEND_PATTERNS: tuple[str, ...] = (
    "src/**",
    "db/**",
    "frontend/**",
    "Dockerfile*",
    "docker-compose*.yml",
)


def _normalize(path: str) -> str:
    """Strip leading ./ and normalize forward slashes."""
    s = path.replace("\\", "/").lstrip("./").lstrip("/")
    return str(PurePosixPath(s)) if s else s


def _match(path: str, pattern: str) -> bool:
    """fnmatch with `**` semantics."""
    if "**" in pattern:
        # Translate `**` → match any number of segments. fnmatch alone
        # treats `*` as "anything except /", so we expand `**` to `*`
        # and feed each path-prefix in.
        prefix, sep, suffix = pattern.partition("**")
        if not sep:
            return fnmatch.fnmatch(path, pattern)
        # Walk the path: any segment break could be the `**` anchor.
        parts = path.split("/")
        for i in range(len(parts) + 1):
            head = "/".join(parts[:i])
            tail = "/".join(parts[i:])
            if not prefix or head.startswith(prefix.rstrip("/")):
                want_suffix = suffix.lstrip("/")
                if not want_suffix or fnmatch.fnmatch(tail, want_suffix):
                    return True
        return False
    return fnmatch.fnmatch(path, pattern)


__all__ = ["ScopePolicy", "MOBILE_PATTERNS", "BACKEND_PATTERNS"]
