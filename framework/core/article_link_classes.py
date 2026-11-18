"""Configurable link classes for the inline-link contract (build item F5a).

`article_link_guard._LINK_RE` hard-codes the path roots it counts (/recipes/,
/k/, /product/, /reviews/, /blog/, /kitchen/category/, /kitchen/). A site
whose money pages live under another root (a power site's /compat/<station>
hubs) cannot ask for "at least 2 /compat/ links" without recompiling that
regex, and every existing site's article INSERT runs through it.
So this is a SEPARATE matcher, compiled from config; `_LINK_RE` is never
touched, and a site that configures no link classes sees no change at all.

Config: the site's block of config/article-link-guard-config.json (the same
file, resolution and storage overlay as the link guard, read through
`article_link_guard.resolve_minima`):

    "link_classes": {
        "product": {"roots": ["/product/"], "min": 5},
        "compat":  {"roots": ["/compat/"],  "min": 2, "own_host_only": true}
    }

A class counts the DISTINCT slugs linked under any of its roots, with exactly
`_LINK_RE`'s link shape: a markdown link `[text](url)` whose url is a
relative path or an absolute URL, the root directly after the host, a slug
`[A-Za-z0-9][A-Za-z0-9-]*` with at most one nested segment, then an optional
query and fragment. A class whose roots are one of the guard's roots
therefore counts exactly what the guard counts for that root
(framework/tests/test_article_link_classes.py proves it on real corpora).
Precedence: among the roots configured across ALL classes, the longest
matching root wins, as in `_LINK_RE`'s alternation. With both /kitchen/ and
/kitchen/category/ configured, /kitchen/category/x counts only toward the
category root; with /kitchen/ alone it counts toward /kitchen/ (slug
"category/x"). `own_host_only` (default false, like `_LINK_RE`) counts
absolute links only when their host is the site's own (`site_root`).

Wiring (build item W2, after the BFCM freeze): the implementer's INSERT gate
and `framework/cli/article_link_guard.py` call `verify_with_guard_config`
for sites that define link classes; `framework/cli/article_publish_gate.py`
already does.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable, Mapping

from framework.core.article_link_guard import resolve_minima, site_hosts_from_root

_ROOT_RE = re.compile(r"^/(?:[A-Za-z0-9][A-Za-z0-9_-]*/)+$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
# The slug / query / fragment shape of article_link_guard._LINK_RE.
_SLUG = r"(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9][A-Za-z0-9-]*)?)"
_TAIL = r"(?:\?[^)]*)?(?:#[^)]*)?\)"


class LinkClassConfigError(ValueError):
    """`link_classes` in the guard config is malformed. A gate fails closed."""


@dataclasses.dataclass(frozen=True)
class LinkClass:
    name: str
    roots: tuple[str, ...]
    min: int = 0
    own_host_only: bool = False


def parse_link_classes(raw: Any) -> tuple[LinkClass, ...]:
    """Validate a `link_classes` mapping ({name: {roots, min, own_host_only}}).
    None / {} is "no classes". Raises LinkClassConfigError on a bad name,
    root ("/compat/": leading and trailing slash) or minimum."""
    if raw in (None, {}, []):
        return ()
    if not isinstance(raw, Mapping):
        raise LinkClassConfigError(f"link_classes must be an object, got {type(raw).__name__}")
    out: list[LinkClass] = []
    for name, spec in raw.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise LinkClassConfigError(f"link_classes: bad class name {name!r}")
        if not isinstance(spec, Mapping):
            raise LinkClassConfigError(f"link_classes.{name}: expected an object")
        roots = spec.get("roots")
        if isinstance(roots, str):
            roots = [roots]
        if not isinstance(roots, (list, tuple)) or not roots:
            raise LinkClassConfigError(f"link_classes.{name}.roots: need at least one root")
        for r in roots:
            if not isinstance(r, str) or not _ROOT_RE.match(r):
                raise LinkClassConfigError(
                    f"link_classes.{name}.roots: {r!r} is not a path root like '/compat/'")
        try:
            minimum = int(spec.get("min", 0) or 0)
        except (TypeError, ValueError):
            raise LinkClassConfigError(f"link_classes.{name}.min: not an integer: {spec.get('min')!r}") from None
        if minimum < 0:
            raise LinkClassConfigError(f"link_classes.{name}.min: must be >= 0")
        out.append(LinkClass(name, tuple(dict.fromkeys(roots)), minimum, bool(spec.get("own_host_only"))))
    return tuple(out)


def compile_matcher(classes: Iterable[LinkClass]) -> "re.Pattern[str] | None":
    """One regex over the union of the classes' roots, longest root first
    (so /kitchen/category/ wins over /kitchen/, as in `_LINK_RE`). None when
    no class has a root."""
    roots = sorted({r for c in classes for r in c.roots}, key=lambda r: (-len(r), r))
    if not roots:
        return None
    alt = "|".join(re.escape(r) for r in roots)
    return re.compile(r"\[[^\]]+\]\((?P<host>https?://[^/)]*)?(?P<root>" + alt + ")" + _SLUG + _TAIL)


def _host_of(prefix: str) -> str:
    return re.sub(r"^https?://", "", prefix or "", flags=re.I).lower()


def class_slugs(body_md: str, classes: Iterable[LinkClass],
                own_hosts: Iterable[str] | None = None) -> dict[str, set[str]]:
    """{class name: distinct slugs linked under its roots}."""
    classes = tuple(classes)
    found: dict[str, set[str]] = {c.name: set() for c in classes}
    rx = compile_matcher(classes)
    if rx is None:
        return found
    hosts = {str(h).lower() for h in (own_hosts or ())}
    by_root: dict[str, list[tuple[str, str]]] = {}
    for m in rx.finditer(body_md or ""):
        by_root.setdefault(m.group("root"), []).append((_host_of(m.group("host") or ""), m.group("slug")))
    for c in classes:
        for root in c.roots:
            for host, slug in by_root.get(root, ()):
                if c.own_host_only and host and host not in hosts:
                    continue
                found[c.name].add(slug)
    return found


def count_link_classes(body_md: str, classes: Iterable[LinkClass],
                       own_hosts: Iterable[str] | None = None) -> dict[str, int]:
    return {name: len(slugs) for name, slugs in class_slugs(body_md, classes, own_hosts).items()}


@dataclasses.dataclass
class LinkClassAudit:
    classes: tuple[LinkClass, ...]
    counts: dict[str, int]
    slugs: dict[str, list[str]]

    @property
    def minima(self) -> dict[str, int]:
        return {c.name: c.min for c in self.classes}

    def shortfalls(self) -> list[tuple[LinkClass, int]]:
        return [(c, self.counts.get(c.name, 0)) for c in self.classes if self.counts.get(c.name, 0) < c.min]

    @property
    def passes(self) -> bool:
        return not self.shortfalls()

    def failure_reason(self) -> str:
        return "; ".join(f"only {have} {c.name} links under {'/'.join(c.roots) if len(c.roots) == 1 else c.roots} "
                         f"(min {c.min})" for c, have in self.shortfalls())

    def addendum(self) -> str:
        """Re-queue text naming each class that is short."""
        short = self.shortfalls()
        if not short:
            return ""
        parts = [f"{have}/{c.min} required distinct links under {' or '.join(c.roots)} (class \"{c.name}\")"
                 for c, have in short]
        return ("LINK CLASSES — VIOLATED. You wrote " + "; ".join(parts) + ". Add inline markdown links woven "
                "into the prose (absolute paths such as [Anchor](" + short[0][0].roots[0] + "<slug>)), "
                "using only pages that exist; do not delete any existing prose.")

    def summary(self) -> str:
        return " ".join(f"{c.name}={self.counts.get(c.name, 0)}/{c.min}" for c in self.classes)

    def to_dict(self) -> dict:
        return {"passes": self.passes, "counts": dict(self.counts), "minima": self.minima,
                "slugs": {k: list(v) for k, v in self.slugs.items()}}


def verify_link_classes(body_md: str, classes: Iterable[LinkClass],
                        own_hosts: Iterable[str] | None = None) -> LinkClassAudit:
    """Counts and verdict for `body_md`. No classes → passes trivially."""
    classes = tuple(classes)
    found = class_slugs(body_md, classes, own_hosts)
    return LinkClassAudit(classes, {k: len(v) for k, v in found.items()},
                          {k: sorted(v) for k, v in found.items()})


def link_classes_from_guard_config(cfg: Mapping[str, Any] | None) -> tuple[LinkClass, ...]:
    """The classes of one site's resolved guard config (`resolve_minima`)."""
    return parse_link_classes((cfg or {}).get("link_classes"))


def resolve_link_classes(site_hint: str, storage=None) -> tuple[LinkClass, ...]:
    """The site's classes from config/article-link-guard-config.json (repo
    default with the storage copy laid over it)."""
    return link_classes_from_guard_config(resolve_minima(site_hint, storage))


def verify_with_guard_config(body_md: str, cfg: Mapping[str, Any] | None) -> LinkClassAudit:
    """`verify_link_classes` with classes and own hosts (`site_root`) from a
    resolved guard config. A site with no `link_classes` passes trivially, so
    calling this for every site is safe."""
    cfg = cfg or {}
    return verify_link_classes(body_md, link_classes_from_guard_config(cfg),
                               site_hosts_from_root(str(cfg.get("site_root") or "")))


def render_link_class_directive(classes: Iterable[LinkClass], site_root: str = "") -> str:
    """The prompt block that tells the writer up front which link classes the
    gate counts. Empty when no class has a minimum."""
    classes = [c for c in classes if c.min > 0]
    if not classes:
        return ""
    root = (site_root or "").rstrip("/")
    lines = ["LINK CLASSES (HARD REQUIREMENT — counted after you exit; the article is not published "
             "short of them):"]
    for c in classes:
        lines.append(f"  at least {c.min} distinct links under {' or '.join(c.roots)} "
                     f"(e.g. [Anchor]({root}{c.roots[0]}<slug>))")
    lines.append("  Weave each link into a sentence at the first mention; link only pages that exist.")
    return "\n".join(lines) + "\n"
