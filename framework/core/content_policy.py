"""Content policy: named, opt-in rule sets for article copy and rendered pages.

Why: a site in a safety-sensitive niche publishes content people act on in an
emergency. A sentence that tells a reader to backfeed a house through the
dryer outlet, or that quotes CPAP runtimes, can kill someone. The existing
honesty guard (`article_claims_guard`) catches invented testing and copied
ratings; it knows nothing about safety. This module is the generic extension
point for that kind of rule (third-site design, binding amendments 1 and 8,
build items F6a and F5a):

  * Rule sets are DATA. `config/content-policy-config.json` (repo default; the
    storage copy at the same key is laid over it) defines named rule sets.
    The engine holds generic English only (negation cues, clause structure);
    every niche word (hazard verbs such as "electrocute", imperative verbs
    such as "backfeed", noun uses such as "power stations", callout labels)
    is config: a rule set's own `negation` vocabulary, or the top-level
    `negation` block.
  * A site gets only the rule sets it opts into (`sites.<site>.rule_sets`).
    The repo default opts nobody in, so every existing site is unaffected.
  * Reject / warn patterns are NEGATION-AWARE, and deliberately strict: a
    prohibition exempts a match only when it GOVERNS it ("Never backfeed",
    "Do not, under any circumstances, backfeed"): the cue sits a few words
    before the match with no clause boundary, subordinator ("if", "when",
    "that"), purpose "to", finite verb or new clause in between. A condition
    after the match turns a prohibition into a permission ("Never backfeed
    without shutting off the main" fails). A hazard statement ("Backfeeding
    can electrocute a line worker") exempts only when the sentence gives no
    instruction. A question is not an instruction, but a how-to or rhetorical
    question is.
  * An exemption does not reach past its own sentence. A CONCESSION in the
    next sentences ("Never backfeed through a dryer outlet. If you
    absolutely must, turn off the main first.", "... Still, with the main
    off, it works.", "Here's how to do it as safely as possible:" + steps)
    is a violation of the rule the warning was about. The openers are
    generic English (`negation.concession_openers`); a rule can name a bare
    subject that opens the same window ("Gas generators belong outdoors. In
    a pinch, the garage works.") with `concession_subjects` and
    `concession_claims`, and name claims that count inside the window with
    no opener at all ("Gas generators belong outdoors. The garage works if
    you leave the door open.") with `concession_bare_claims`. Not caught: a
    concession in the warning's own sentence ("Gas generators belong
    outdoors, but the garage works if you leave the door open.") or more
    than `concession_window` sentences after it, or across a heading.
  * A list exempts its items only under an explicit prohibition lead-in
    ("What not to do", "Never do any of these:"), never an ordered list.
  * A safety-callout block (a blockquote that opens with "**Safety:**",
    "Warning:", ... or a rendered element with a safety-callout class) may
    MENTION banned practices. It may not carry instructions or permissions.
  * Claim rules (`negation_target: "claim"`, e.g. medical) judge the CLAIM, not
    the subject: a CPAP mention is fine, a runtime / wattage / "it runs it"
    claim next to it is not, in the same sentence, the same paragraph, list
    item or table row, or a section whose heading names the device. An
    article's title is a level-1 heading over the whole article, as the
    rendered page's <h1> is, so a title that names the device puts every
    sentence of the article in scope.
  * Required notices: when a trigger is discussed (whole-home / furnace /
    sump connection) a notice must appear somewhere on the page (the
    licensed-electrician notice). A negated notice ("you don't need a
    licensed electrician") does not count; "never ... without a licensed
    electrician" does.

Entry points:

  resolve_policy(site_hint, storage=..., config=..., rule_sets=...) -> ContentPolicy
  check_article(policy, body_md=..., title=..., subtitle=..., excerpt=..., faqs=...)
  check_blocks(policy, blocks)                -> ContentPolicyAudit
  markdown_blocks(text, field=..., ...)       -> list[Block]   (HTML lives in
                                                  framework.core.content_policy_audit)

The policy DETECTS; it never rewrites prose (editorial prose is Opus-only).
`ContentPolicyAudit.addendum()` is the re-queue text the publish gate
(`framework.cli.article_publish_gate`) hands the author.

Fail closed: a config that names an unknown rule set, an unknown action or an
invalid regex, or a storage copy that exists but cannot be read, raises
ContentPolicyConfigError. Callers that gate publishing treat that as "do not
publish", never as "no rules".
"""
from __future__ import annotations

import copy
import dataclasses
import functools
import html as _html
import json as _json
import pathlib as _pathlib
import re
import sys as _sys
from typing import Any, Iterable, Mapping, Sequence

CONFIG_KEY = "config/content-policy-config.json"
_REPO_CONFIG = _pathlib.Path(__file__).resolve().parents[2] / "config" / "content-policy-config.json"

ACTIONS = ("reject", "warn", "off")
NEGATION_TARGETS = ("anchor", "claim")
# Fields of an article. Rendered pages map onto the same names
# (content_policy_audit): <title>/<h1> -> title, meta descriptions -> excerpt,
# page text and JSON-LD -> body.
ARTICLE_FIELDS = ("title", "subtitle", "excerpt", "body", "faqs")

DEFAULT_CALLOUTS: dict = {
    # A blockquote whose first line (emphasis removed) matches this is a
    # safety callout.
    "markdown_label_re": (r"^\W{0,4}(?:safety(?:\s+(?:note|notice|warning|first))?|warning|caution|danger)"
                          r"\s*[:.!—–-]"),
    # Rendered pages: an element whose class list matches, or whose attributes
    # match html_attr_re, is a safety callout (content_policy_audit).
    "html_class_re": r"(?:^|\s)(?:safety-callout|callout-safety|content-policy-safety)(?:\s|$)",
    "html_attr_re": r"\bdata-(?:content-policy|callout)\s*=\s*[\"']?safety",
    # Rendered pages: an element whose attributes match this is third-party or
    # spec data the site does not author (an Amazon title, a spec table); it
    # and its subtree are not scanned. Authored copy may never carry it: the
    # article check rejects a body or FAQ that does.
    "html_skip_re": r"\bdata-content-policy\s*=\s*[\"']?(?:skip|source)\b",
    # JSON-LD nodes of these @types are product / navigation data, not copy.
    "jsonld_skip_types": ["Product", "ProductGroup", "Offer", "AggregateOffer", "ItemList", "ListItem",
                          "BreadcrumbList", "Organization", "Brand", "Person", "ImageObject", "WebSite",
                          "SearchAction", "AggregateRating", "Rating", "Review"],
    # A "callout" longer than this is ordinary text: a callout exempts short
    # warnings, it is not a place to hide a how-to.
    "max_chars": 700,
}


class ContentPolicyConfigError(ValueError):
    """The content-policy config cannot be compiled (unknown rule set or
    action, invalid regex, malformed rule) or its storage copy cannot be
    read. Gate callers fail closed."""


# ── Negation lexicon: generic English. Niche vocabulary is config. ──────────
# Every list below is the code default for one `negation` config key. The
# top-level `negation` block may REPLACE a list (`<key>`) or ADD to it
# (`extra_<key>`); a rule set's own `negation` block may only add
# (`extra_<key>`), and only while a site uses that rule set.

_APOS = "['’]"
_STRONG_CUES = [
    r"never", r"do\s+not", rf"don{_APOS}t", r"dont", r"must\s+not", rf"mustn{_APOS}t",
    r"should\s+not", rf"shouldn{_APOS}t", r"cannot", rf"can{_APOS}t", r"can\s+not",
    r"will\s+not", rf"won{_APOS}t", r"avoid(?:s|ed|ing)?", r"under\s+no\s+circumstances",
    r"no\s+one\s+should", r"nobody\s+should", r"refuse\s+to", r"instead\s+of", r"rather\s+than",
    r"in\s+place\s+of", r"illegal", r"unsafe", r"dangerous", r"deadly", r"lethal",
    rf"(?:not|never|isn{_APOS}t|aren{_APOS}t)\s+(?:ever\s+)?(?:safe|legal|allowed|permitted|ok|okay)",
    r"prohibited", r"forbidden", r"banned", r"against\s+(?:the\s+)?(?:law|rules)",
    r"no\s+safe\s+way", r"there\s+is\s+no", rf"there{_APOS}s\s+no",
    r"a\s+(?:bad|terrible|dangerous)\s+idea", r"reckless", r"prevent(?:s|ed|ing)?",
    r"(?:protect|guard)(?:s|ed|ing)?\s+against",
    "[❌\U0001f6ab⛔✗✘]",       # cross / no-entry marks in "don'ts" lists
]
# Weak negations reach only 3 words ("no double-male cords", "is not a
# backfeed kit").
_WEAK_CUES = [
    r"not", r"no", r"nor", r"neither", r"none", r"without", r"unlike", rf"isn{_APOS}t", rf"aren{_APOS}t",
    rf"wasn{_APOS}t", rf"weren{_APOS}t", rf"doesn{_APOS}t", r"does\s+not", rf"didn{_APOS}t",
    r"did\s+not",
]
# "You don't need an electrician to <X>" grants permission for X. Only a
# permission phrase that sits right before the match ("you don't have to
# backfeed") negates it.
_PERMISSION = [rf"(?:(?:don{_APOS}t|do\s+not|doesn{_APOS}t|does\s+not|won{_APOS}t|will\s+not|never|no)"
               r"\s+(?:need|needs|require|requires|have\s+to|has\s+to)\b"
               r"|\bno\s+need\s+(?:for|to)\b|\bnot\s+(?:necessary|required|needed)\b)"]
# The only words that may sit between a permission phrase and the practice it
# negates: "don't have to backfeed", "don't need to use a double-male cord".
_PERMISSION_GAP_WORDS = frozenset({"to", "a", "an", "any", "the", "your", "use", "buy", "own"})
# Idioms whose negation words negate nothing.
_IDIOMS = [
    r"never\s+been\s+(?:easier|simpler|cheaper|better|faster|safer|more\s+\w+)",
    rf"can{_APOS}t\s+(?:go\s+wrong|beat)", rf"couldn{_APOS}t\s+be\s+(?:easier|simpler)",
    rf"(?:don{_APOS}t|do\s+not)\s+(?:hesitate|forget|miss|worry|wait|sweat|settle)",
    r"never\s+(?:mind|miss|run\s+out|settle|worry|forget|fail|hesitate)",
    rf"(?:never|don{_APOS}t|do\s+not)\s+(?:ever\s+)?(?:be\s+(?:afraid|scared|shy|too\s+\w+)|fear|neglect|fail)\s+to",
    rf"(?:never|don{_APOS}t|do\s+not)\s+(?:lose|be\s+without|be\s+left|go\s+without|sit|freeze)\b",
    rf"(?:never|don{_APOS}t|do\s+not)\s+let\s+(?:[\w'’-]+\s+){{0,4}}?(?:stop|keep|hold\s+back|prevent)\b",
    r"no\s+(?:problem|wonder|doubt|matter(?:\s+what)?|reason\s+not\s+to|more|longer)", r"not\s+only",
    r"nothing\s+(?:beats|is\s+easier|stops|prevents|keeps)",
    rf"(?:can{_APOS}t|won{_APOS}t|doesn{_APOS}t|never)\s+hurts?\s+to",
    # reported denial: "anyone who says you can't backfeed" is a permission
    rf"(?:says?|said|claims?|thinks?|believes?|assumes?|tells?\s+you|told\s+you|insists?)\s+(?:that\s+)?"
    rf"(?:you|it|one|we)\s+(?:can{_APOS}t|cannot|can\s+not|shouldn{_APOS}t|mustn{_APOS}t)",
]
# A denial of danger is a permission: "it's not dangerous to backfeed".
_DENIAL = (rf"(?:not|never|isn{_APOS}t|aren{_APOS}t|no\s+longer|hardly)\s+"
           r"(?:that\s+|very\s+|really\s+|actually\s+|all\s+that\s+)?"
           r"(?:dangerous|unsafe|illegal|risky|hard|difficult|complicated|a\s+problem|an\s+issue|a\s+big\s+deal)")
# Hazard vocabulary. A state after a copula ("is illegal"), a harm after a
# modal ("can kill"), or a free-form predicate, AFTER the match, turns a
# mention into a warning.
_HAZARD_STATES = [
    r"illegal", r"dangerous", r"deadly", r"lethal", r"fatal", r"unsafe", r"hazardous", r"risky", r"reckless",
    r"prohibited", r"forbidden", r"banned", r"a\s+(?:safety|health)\s+(?:hazard|risk)",
    r"against\s+(?:the\s+)?(?:law|rules)",
]
_HAZARD_HARMS = [
    r"kill\w*", r"injur\w*", r"hurt", r"start\s+(?:a\s+)?fires?", r"cause\s+(?:a\s+)?fires?", r"explod\w*",
    r"destroy\w*", r"void\s+(?:\w+\s+)?warrant\w*",
]
_HAZARD_PREDICATES = [
    r"\b(?:is|are|was|were)\s+(?:not|never)\s+(?:ever\s+|really\s+|actually\s+)?(?:safe|legal|allowed|permitted|ok|okay)\b",
    rf"\b(?:isn{_APOS}t|aren{_APOS}t|wasn{_APOS}t)\s+(?:ever\s+|really\s+|actually\s+)?"
    r"(?:safe|legal|allowed|permitted|ok|okay)\b",
    r"\b(?:kills?|causes?\s+fires?|starts?\s+fires?)\b",
    r"\b(?:puts?|putting)\s+(?:\w+\s+){0,3}(?:at\s+risk|in\s+danger)",
    r"\b(?:violates?|breaks?)\s+(?:\w+\s+){0,2}(?:law|laws|rules)\b",
]
# Imperative verbs that open an instruction. A callout exempts mentions, not
# instructions; a comma followed by one of these starts a new clause.
_IMPERATIVE_VERBS = [
    "plug", "unplug", "connect", "disconnect", "run", "use", "install", "wire", "hook", "turn", "remove", "open",
    "bypass", "feed", "put", "set", "attach", "flip", "switch", "tie", "jump", "disable", "buy", "get", "grab",
    "try", "add", "mount", "charge", "take", "make", "keep", "leave", "start", "go", "check", "call", "hire",
    "ask", "cut", "strip", "insert", "push", "pull", "place", "move", "power", "drill", "short", "bridge",
    "override", "defeat", "wait", "clip", "bolt", "screw", "route", "swap", "replace", "skip", "ignore", "pick",
    "order", "set\\s+up", "shut", "close", "flick", "hold", "press", "rig", "tape", "cover",
]
# Words after an imperative-looking verb that make it a noun ("power
# stations"). Niche nouns are config.
_NOUN_EXCLUSIONS: list[str] = []
# Instruction clauses that recommend the safe alternative ("hire a licensed
# electrician") and so do not undo a hazard statement. Niche: config.
_SAFE_INSTRUCTIONS: list[str] = []
# Openers of a CONCESSION: a sentence that, right after a prohibition or a
# hazard statement, goes on to tell the reader how to do the banned thing
# anyway ("Never backfeed. IF YOU ABSOLUTELY MUST, turn off the main
# first."). Matched at the start of a sentence (after "but" / "and" / "so").
# Each opener concedes the practice itself: "If you must," (the practice
# elided) or "If you must run IT in the garage," (named by a pronoun), never
# a fresh condition ("If you need whole-home power, ...", "If you have to
# evacuate, ...").
_CONCESSION_OPENERS = [
    rf"if\s+you\s+(?:(?:absolutely|really|truly|still|simply|just|ever)\s+)?"
    rf"(?:must|have\s+to|need\s+to|insist|do|go\s+ahead|can{_APOS}t\s+avoid\s+it|cannot\s+avoid\s+it)"
    r"(?:\s+(?:do\s+)?(?:it|so|this|that))?(?:\s+anyway)?(?=\s*[,;:—–]|\s+-\s|\s*$)",
    rf"if\s+you\s+(?:(?:absolutely|really|truly|still)\s+)?(?:must|insist\s+on|are\s+going\s+to"
    rf"|{_APOS}re\s+going\s+to|still\s+want\s+to|decide\s+to|choose\s+to)\s+(?:[\w'’-]+\s+){{0,2}}?"
    r"(?:it|one|them|this|that)\b",
    r"if\s+you\s+(?:do|try)\s+(?:it|this|that|so)\s+anyway\b",
    r"if\s+you\s+(?:ignore|disregard)\s+(?:this|that|it|the\s+\w+|our\s+\w+)\b",
    rf"if\s+(?:there\s+is|there{_APOS}s|you\s+have)\s+no\s+(?:other\s+)?(?:way|option|choice|alternative)\b",
    r"if\s+all\s+else\s+fails\b",
    r"in\s+a\s+pinch\b", r"as\s+a\s+(?:last|final)\s+resort\b",
    r"(?:that|this)\s+said\b", r"having\s+said\s+(?:that|this)\b", r"with\s+(?:that|this)\s+said\b",
    r"still\s*,", r"even\s+so\b", r"nonetheless\b", r"nevertheless\b",
    r"at\s+(?:the\s+(?:very\s+)?)?least\b",
    rf"here(?:{_APOS}s|\s+is)\s+how\s+(?:to\s+|you\s+(?:can\s+|could\s+|should\s+)?)?(?:do|try|manage|attempt)"
    r"\s+(?:it|this|that|so)\b",
    r"(?:the\s+)?(?:safest|least\s+dangerous|least\s+risky|best)\s+way\s+to\s+do\s+(?:it|this|that|so)\b",
]

_LIST_KEYS = ("strong_cues", "weak_cues", "permission_phrases", "idioms", "hazard_states", "hazard_harms",
              "hazard_predicates", "imperative_verbs", "noun_exclusions", "safe_instructions",
              "concession_openers")
_LEXICON_DEFAULTS: dict[str, list[str]] = {
    "strong_cues": _STRONG_CUES, "weak_cues": _WEAK_CUES, "permission_phrases": _PERMISSION,
    "idioms": _IDIOMS, "hazard_states": _HAZARD_STATES, "hazard_harms": _HAZARD_HARMS,
    "hazard_predicates": _HAZARD_PREDICATES, "imperative_verbs": _IMPERATIVE_VERBS,
    "noun_exclusions": _NOUN_EXCLUSIONS, "safe_instructions": _SAFE_INSTRUCTIONS,
    "concession_openers": _CONCESSION_OPENERS,
}
_NEGATION_KEYS = (set(_LIST_KEYS) | {f"extra_{k}" for k in _LIST_KEYS}
                  | {"max_gap_words", "concession_window", "_comment", "description"})
DEFAULT_MAX_GAP_WORDS = 4
# How many sentences after a prohibition (or a hazard statement, ...) a
# concession may follow it in the same section: "Do not backfeed. It can kill
# line workers. STILL, with the main off, it works."
DEFAULT_CONCESSION_WINDOW = 2

_FILLER = r"(?:(?!not\b|never\b|no\b)[\w'’-]+\s+){0,4}?"
# A clause opened by one of these, followed by a hazard in the clause after
# it, is a cautionary conditional: "If someone backfeeds through a dryer
# outlet, line workers can be electrocuted."
_CONDITIONAL_OPENERS = re.compile(
    r"^\W*(?:if|when|whenever|anyone\s+who|anybody\s+who|people\s+who|someone\s+who|those\s+who)\b", re.I)
# A comma ends a clause when what follows starts a new clause. These openers
# make the clause before the comma subordinate, so its comma always ends it.
_LEADING_SUBORDINATE = re.compile(
    r"^\W*(?:if|when|whenever|unless|although|though|since|because|while|once|after|before|until"
    r"|as\s+long\s+as|in\s+case|without|with|unlike|for|to|in|on|during|despite|instead\s+of)\b", re.I)
_SUBJECT_START = re.compile(
    r"^\W*(?:you|we|i|it|they|he|she|this|that|these|those|there|anyone|someone|everyone|everybody"
    r"|nobody|people|which|who)\b", re.I)
_FINITE_VERB = re.compile(
    rf"\b(?:is|are|was|were|be|been|being|has|have|had|can|could|will|would|should|must|may|might|shall"
    rf"|do|does|did|isn{_APOS}t|aren{_APOS}t|can{_APOS}t|won{_APOS}t|don{_APOS}t|doesn{_APOS}t"
    rf"|didn{_APOS}t|it{_APOS}s|that{_APOS}s|there{_APOS}s|you{_APOS}re|you{_APOS}ll|you{_APOS}ve"
    rf"|we{_APOS}re|works|runs|powers|keeps|lets|makes|needs|gets|goes|takes|gives|helps|uses|handles"
    rf"|supports|lasts|starts|feeds|connects|plugs|stays|saves|costs|means|requires|allows)\b", re.I)
# A finite verb or modal inside the gap between a cue and the match starts a
# new predicate the cue does not govern ("Never say you CAN backfeed").
_GAP_FINITE = re.compile(
    rf"^(?:can|could|will|would|should|must|may|might|shall|is|are|was|were|has|have|had|does|did"
    rf"|isn{_APOS}t|aren{_APOS}t|wasn{_APOS}t|weren{_APOS}t|hasn{_APOS}t|haven{_APOS}t|doesn{_APOS}t"
    rf"|didn{_APOS}t|can{_APOS}t|won{_APOS}t|wouldn{_APOS}t|shouldn{_APOS}t|couldn{_APOS}t"
    rf"|it{_APOS}s|that{_APOS}s|there{_APOS}s|you{_APOS}re|you{_APOS}ll|you{_APOS}ve|we{_APOS}re"
    rf"|they{_APOS}re|i{_APOS}m)$", re.I)
# Words that end a cue's scope when they sit between the cue and the match.
_SCOPE_BREAKERS = frozenset({
    "if", "when", "whenever", "because", "since", "so", "until", "till", "unless", "before", "after", "while",
    "once", "although", "though", "whereas", "where", "that", "which", "who", "whom", "whose", "but", "yet",
    "then", "otherwise", "except", "without",
})
# "never TRY to backfeed": the only words a cue may reach across before an
# infinitive "to". Anything else makes the "to" a purpose clause the cue does
# not govern ("Never pay an electrician to backfeed").
_CONTROL_WORDS = frozenset({
    "ever", "try", "trying", "attempt", "attempting", "want", "choose", "decide", "dare", "be", "tempted",
    "going", "allowed", "permitted", "able", "plan", "planning", "need", "have", "ought", "advisable", "wise",
    "smart", "safe", "legal", "okay", "ok", "idea", "way", "reason", "good", "a", "it", "is", "ever",
})
# A condition AFTER a governed match turns a prohibition into a conditional
# permission: "Never backfeed WITHOUT shutting off the main", "Don't backfeed
# UNTIL the main is off". "even if" / "even when" are emphasis, not lifts.
_LIFT_WORDS = (r"without|until|till|unless|before|except|other\s+than|only\s+(?:if|when|after|with|once)"
               r"|as\s+long\s+as|so\s+long\s+as|provided(?:\s+that)?|if|when|whenever|while")
_LIFT = re.compile(rf"(?<!even\s)\b(?:{_LIFT_WORDS})\b", re.I)
_LIFT_START = re.compile(rf"^\W*(?:{_LIFT_WORDS})\b", re.I)
_CONTRASTIVE = re.compile(
    r"\b(?:but|however|instead(?!\s+of)|yet|although|though|unless|except|whereas|otherwise)\b", re.I)
_CONTRAST_START = re.compile(r"^\W*(?:but|however|yet|though|although|still|even\s+so)\b", re.I)
_HARD_SEPARATOR = re.compile(r";|:(?=\s|$)|—|\s–\s|\s-\s|\s\|\s|\|")
_RELATIVE = re.compile(r"\b(?:that|which|who|whose|whom)\b", re.I)
_MODAL = re.compile(r"\b(?:can|could|will|would|may|might|lets?\s+you|allows?\s+you)\b", re.I)
_CUE_ONLY_SEGMENT = re.compile(
    rf"^\W*(?:never|ever|do|not|don{_APOS}t|please|under|any|circumstances|again|really|seriously"
    rf"|absolutely|you|should|must)(?:\W+(?:never|ever|do|not|don{_APOS}t|please|under|any"
    rf"|circumstances|again|really|seriously|absolutely|you|should|must))*\W*$", re.I)
_PROHIBITION_START = re.compile(
    rf"^\W*(?:(?:and|so|then|or|but|also|please|just|and\s+then)\W+){{0,2}}"
    rf"(?:never|do\s+not|don{_APOS}t|dont|avoid|stop|no\b|nobody|no\s+one|under\s+no)", re.I)
# "is safe / is OK / is fine" about a practice is a permission, never a mention.
_PERMISSIVE = re.compile(
    rf"(?<!not\s)(?<!never\s)\b(?:is|are|it{_APOS}s|its|be|remains?|becomes?)\s+"
    r"(?:perfectly\s+|totally\s+|completely\s+|usually\s+|generally\s+|only\s+|still\s+|actually\s+|really\s+)?"
    r"(?:safe|ok|okay|fine|allowed|legal|permitted|acceptable)\b", re.I)
# Questions that are how-to framing or rhetorical endorsements are not exempt.
_QUESTION_NOT_EXEMPT = re.compile(
    rf"^\W*how\s+(?:do|can|could|should|would|to)\b|\bwhy\s+(?:not|pay|bother|spend|wait|buy|hire|waste)\b"
    rf"|\bwhen\s+you\s+(?:can|could)\b|\bwhat{_APOS}s\s+stopping\b|\bwho\s+needs\b", re.I)
# "You can't X without Y" says Y is NEEDED for X: no negation of Y.
_CANT_WITHOUT = re.compile(rf"\b(?:can{_APOS}t|cannot|can\s+not|won{_APOS}t|impossible|no\s+way)\b", re.I)
_WORD = re.compile(r"[\w'’-]+")
_GAP_TOKEN = re.compile(r"[\w'’-]+|,")


def _alt(words: Sequence[str]) -> str:
    return "|".join(f"(?:{w})" for w in words)


def _compile(pattern: Any, where: str) -> re.Pattern:
    if not isinstance(pattern, str) or not pattern:
        raise ContentPolicyConfigError(f"{where}: pattern must be a non-empty string, got {pattern!r}")
    try:
        return re.compile(pattern, re.I)
    except re.error as e:
        raise ContentPolicyConfigError(f"{where}: invalid regex {pattern!r}: {e}") from None


def _pattern_list(v: Any, where: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        raise ContentPolicyConfigError(f"{where}: expected a list of regexes, got {type(v).__name__}")
    for p in v:
        _compile(p, where)
    return [str(p) for p in v]


@dataclasses.dataclass(frozen=True)
class _Lexicon:
    strong: re.Pattern
    weak: re.Pattern
    permission: re.Pattern
    idioms: re.Pattern
    denial: re.Pattern
    hazard: re.Pattern
    imperative_start: re.Pattern       # a clause that opens with an imperative (comma boundaries)
    instruction_start: re.Pattern      # a clause that gives an instruction (imperative or "you should <verb>")
    safe_instructions: tuple[re.Pattern, ...] = ()
    max_gap: int = DEFAULT_MAX_GAP_WORDS
    concession: re.Pattern = re.compile(r"(?!x)x")   # a sentence that opens with a concession opener
    concession_window: int = DEFAULT_CONCESSION_WINDOW

    @classmethod
    def build(cls, extra: Mapping[str, Any] | None = None, *,
              rule_set_vocab: Sequence[tuple[str, Mapping[str, Any]]] = ()) -> "_Lexicon":
        """The generic defaults, then the top-level `negation` block (`<key>`
        replaces, `extra_<key>` adds), then each active rule set's own
        vocabulary (`extra_<key>` only)."""
        extra = dict(extra or {})
        unknown = sorted(set(extra) - _NEGATION_KEYS)
        if unknown:
            raise ContentPolicyConfigError(f"negation: unknown key(s) {unknown} (known: {sorted(_NEGATION_KEYS)})")
        lists = {k: list(v) for k, v in _LEXICON_DEFAULTS.items()}
        for k in _LIST_KEYS:
            if k in extra:
                lists[k] = _pattern_list(extra[k], f"negation.{k}")
            lists[k] += _pattern_list(extra.get(f"extra_{k}"), f"negation.extra_{k}")
        for name, vocab in rule_set_vocab:
            vocab = dict(vocab or {})
            bad = sorted(k for k in vocab if k not in {f"extra_{x}" for x in _LIST_KEYS} | {"_comment"})
            if bad:
                raise ContentPolicyConfigError(
                    f"rule_sets.{name}.negation: only extra_<key> vocabulary is allowed, got {bad}")
            for k in _LIST_KEYS:
                lists[k] += _pattern_list(vocab.get(f"extra_{k}"), f"rule_sets.{name}.negation.extra_{k}")
        try:
            max_gap = int(extra.get("max_gap_words", DEFAULT_MAX_GAP_WORDS))
        except (TypeError, ValueError):
            raise ContentPolicyConfigError(f"negation.max_gap_words: not an integer: "
                                           f"{extra.get('max_gap_words')!r}") from None
        if not 1 <= max_gap <= 12:
            raise ContentPolicyConfigError("negation.max_gap_words: must be between 1 and 12")
        try:
            window = int(extra.get("concession_window", DEFAULT_CONCESSION_WINDOW))
        except (TypeError, ValueError):
            raise ContentPolicyConfigError(f"negation.concession_window: not an integer: "
                                           f"{extra.get('concession_window')!r}") from None
        if not 0 <= window <= 6:
            raise ContentPolicyConfigError("negation.concession_window: must be between 0 (off) and 6")
        verbs = _alt(lists["imperative_verbs"]) or r"(?!x)x"
        nouns = _alt(lists["noun_exclusions"])
        not_noun = r"(?![-'’]" + (rf"|[ \t]+(?:{nouns})\b" if nouns else "") + ")"
        hazard = (
            r"\b(?:is|are|was|were|remains?|becomes?|can\s+be|could\s+be|would\s+be|will\s+be|gets?)\s+" + _FILLER
            + rf"(?:{_alt(lists['hazard_states']) or '(?!x)x'})\b"
            + r"|\b(?:can|could|will|would|may|might|does|do)\s+" + _FILLER
            + rf"(?:{_alt(lists['hazard_harms']) or '(?!x)x'})"
            + "".join(f"|(?:{p})" for p in lists["hazard_predicates"]))
        prefix = (r"(?:(?:and|so|then|or|but|first|next|now|finally|also|always|just|simply|only|carefully"
                  r"|immediately|quickly|please|instead|still|otherwise|step\s+\d+)\W+){0,3}")
        try:
            return cls(
                strong=re.compile(rf"(?<![\w'’])(?:{_alt(lists['strong_cues'])})(?![\w'’])", re.I),
                weak=re.compile(rf"(?<![\w'’])(?:{_alt(lists['weak_cues'])})(?![\w'’])", re.I),
                permission=re.compile(_alt(lists["permission_phrases"]) or r"(?!x)x", re.I),
                idioms=re.compile(rf"\b(?:{_alt(lists['idioms']) or '(?!x)x'})\b", re.I),
                denial=re.compile(rf"\b{_DENIAL}\b", re.I),
                hazard=re.compile(hazard, re.I),
                imperative_start=re.compile(
                    rf"^\W*(?:(?:first|then|next|now|finally|also|and|so|step\s+\d+)\W+)?(?:{verbs})\b{not_noun}",
                    re.I),
                instruction_start=re.compile(
                    rf"^\W*{prefix}(?:(?:be\s+sure|make\s+sure|remember|try)\s+(?:to\s+|you\s+)?)?"
                    rf"(?:{verbs})\b{not_noun}"
                    rf"|^\W*{prefix}(?:you|we)\s+(?:should|must|need\s+to|have\s+to|{_APOS}ll\s+(?:want|need)\s+to"
                    rf"|will\s+(?:want|need)\s+to|ought\s+to)\s+(?:always\s+|just\s+|first\s+|then\s+|simply\s+)?"
                    rf"(?:{verbs})\b{not_noun}", re.I),
                safe_instructions=tuple(re.compile(p, re.I) for p in lists["safe_instructions"]),
                max_gap=max_gap,
                concession=re.compile(
                    rf"^\W*(?:(?:but|and|so|or|then|now|well|yet|ok|okay)\W+)?"
                    rf"(?:{_alt(lists['concession_openers']) or '(?!x)x'})", re.I),
                concession_window=window,
            )
        except re.error as e:  # the lists compiled one by one; their union must too
            raise ContentPolicyConfigError(f"negation vocabulary does not compile: {e}") from None


_DEFAULT_LEXICON = _Lexicon.build()


# ── Text model ─────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Block:
    """One unit of visible text. `kind` is heading | paragraph | list_item |
    table_row | quote | code | title | meta. Headings carry `level` (an FAQ
    question is a level-7 heading over its answer). `ancestors` are the
    indexes of the headings whose sections contain the block."""
    field: str
    kind: str
    text: str
    line: int = 0
    level: int = 0
    callout: bool = False
    negated_lead: bool = False
    ancestors: tuple[int, ...] = ()


_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\([^)]*\)")
_MD_REFLINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_MD_AUTOLINK = re.compile(r"<(?:https?://|mailto:)[^>]+>")
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def inline_text(md: str) -> str:
    """Markdown inline syntax reduced to the words a reader sees: links keep
    their anchor text (never their URL), images their alt text; emphasis,
    code ticks, tags and entities are removed."""
    t = _HTML_COMMENT.sub(" ", md or "")
    t = _MD_IMAGE.sub(r"\1", t)
    for _ in range(2):                      # one level of nested brackets
        t = _MD_LINK.sub(r"\1", t)
    t = _MD_REFLINK.sub(r"\1", t)
    t = _MD_AUTOLINK.sub(" ", t)
    t = _HTML_TAG.sub(" ", t)
    t = t.replace("**", "").replace("__", "").replace("~~", "").replace("`", "")
    t = re.sub(r"(?<![\w*])\*(?=\S)|(?<=\S)\*(?![\w*])", "", t)
    t = re.sub(r"(?<!\w)_(?=\S)|(?<=\S)_(?!\w)", "", t)
    t = t.replace("\\|", "|")
    t = _html.unescape(t)
    return re.sub(r"[ \t ]+", " ", t).strip()


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING_LINE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_LINE = re.compile(r"^(\s*)(?:([-*+•])|\d{1,9}[.)])\s+(.*)$")
_QUOTE_LINE = re.compile(r"^\s{0,3}>\s?(.*)$")
# GFM, as the kit renderer reads it (kits/affiliate-site/server/src/render/md.ts):
# a header row holding an unescaped pipe, then a delimiter row holding a pipe;
# outer pipes optional; `\|` is a literal pipe.
_TABLE_DELIM = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_UNESCAPED_PIPE = re.compile(r"(?:^|[^\\])\|")
_HR_LINE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$")
_RAW_HTML_LINE = re.compile(r"^\s*<(?!/?(?:em|strong|code|b|i|a|span|br)\b)(?!https?:)[a-zA-Z]")

# A list lead-in negates its items only when it is itself a prohibition label
# or a prohibition that points at the list. Generic English.
_LEAD_LABEL = re.compile(
    r"^\W*(?:what\s+not\s+to\s+(?:do|try|use)|what\s+to\s+avoid"
    r"|things\s+(?:to\s+avoid|not\s+to\s+do|(?:you\s+should\s+)?never\s+(?:to\s+)?do)"
    r"|(?:(?:common|dangerous|deadly|unsafe|costly|big|biggest|top|\d+)\s+)?mistakes(?:\s+(?:to\s+avoid|people\s+make))?"
    rf"|don{_APOS}?ts|never\s+do\s+(?:this|these|that)|do\s+not\s+do\s+(?:this|these|that)"
    r"|(?:dangerous|unsafe|deadly|illegal|risky|bad)\s+(?:practices|hacks|shortcuts|mistakes|ideas|setups|habits|advice)"
    r")\b", re.I)
_LEAD_PROHIBITION = re.compile(
    rf"^\W*(?:never|do\s+not|don{_APOS}t|dont|avoid|stop)\b"
    r"(?:\s+(?:ever|do|doing|try|trying|attempt|attempting|use|using))*"
    r"\s+(?:any\s+of\s+|one\s+of\s+|either\s+of\s+|all\s+of\s+)?(?:these|this|the\s+following|the\s+below|those)\b",
    re.I)
_LEAD_DENY = re.compile(r"\b(?:steps?|how\s+to|instead|then|but|however|here['’]?s|plan|guide|method|setup"
                        r"|tutorial|walkthrough)\b", re.I)


def lead_in_prohibits(prev: Block | None) -> bool:
    """Is `prev` a prohibition lead-in for the list after it ("What not to
    do", "Never do any of these during an outage:")? A heading counts as is;
    a paragraph or quote only when it ends with ':'. A heading that merely
    contains a negative word ("Never lose power again", "Avoid the cold:
    whole-home hookup steps") does not. A page or article title (a
    title-field heading) never does: on the rendered page the disclosure
    and the dek sit between the <h1> and the body, so a title never
    introduces the list that opens a body."""
    if prev is None or prev.kind not in ("heading", "paragraph", "quote") or prev.field == "title":
        return False
    t = prev.text.strip()
    if prev.kind != "heading" and not t.endswith(":"):
        return False
    t = t.rstrip(":").strip()
    if not t or len(_words(t)) > 14 or _LEAD_DENY.search(t):
        return False
    return bool(_LEAD_LABEL.match(t) or _LEAD_PROHIBITION.match(t))


class BlockBuilder:
    """Collects Blocks for one document and tracks heading sections, list
    runs and their lead-ins. Shared by the markdown splitter here and the
    HTML extractor (framework.core.content_policy_audit)."""

    def __init__(self, out: list[Block], field: str, callouts: Mapping[str, Any] | None = None):
        callouts = callouts or DEFAULT_CALLOUTS
        self.out, self.field = out, field
        self.label_re = re.compile(str(callouts.get("markdown_label_re") or DEFAULT_CALLOUTS["markdown_label_re"]),
                                   re.I)
        self.max_chars = int(callouts.get("max_chars") or DEFAULT_CALLOUTS["max_chars"])
        self.stack: list[tuple[int, int]] = []          # (level, block index)
        self.in_list_run = False
        self.run_ordered = False
        self.lead_negated = False

    def ancestors(self) -> tuple[int, ...]:
        return tuple(i for _lvl, i in self.stack)

    def add(self, kind: str, text: str, line: int = 0, *, level: int = 0, callout: bool = False,
            field: str | None = None, raw: bool = False, ordered: bool = False) -> int:
        """Append one block; returns its index, or -1 when it has no text.
        `raw` skips the markdown inline cleanup (text that is already plain,
        e.g. from an HTML parser); whitespace is still collapsed. `ordered`
        marks a numbered list item: a lead-in never exempts numbered steps."""
        field = field or self.field
        if kind == "code":
            clean = (text or "").strip()
        elif raw:
            clean = re.sub(r"\s+", " ", text or "").strip()
        else:
            clean = inline_text(text)
        if not clean:
            return -1
        if kind == "list_item":
            if not self.in_list_run or ordered != self.run_ordered:
                prev = self.out[-1] if self.out else None
                self.lead_negated = (not ordered and prev is not None and prev.kind != "list_item"
                                     and lead_in_prohibits(prev))
            self.in_list_run, self.run_ordered = True, ordered
        else:
            self.end_list_run()
        if kind == "heading":
            while self.stack and self.stack[-1][0] >= level:
                self.stack.pop()
            self.out.append(Block(field, kind, clean, line, level, callout, False, self.ancestors()))
            self.stack.append((level, len(self.out) - 1))
            return len(self.out) - 1
        self.out.append(Block(field, kind, clean, line, 0, callout,
                              self.lead_negated if kind == "list_item" else False, self.ancestors()))
        return len(self.out) - 1

    def end_list_run(self) -> None:
        self.in_list_run = False
        self.lead_negated = False

    def close_sections(self, level: int) -> None:
        """End the sections of headings at `level` or deeper (an FAQ question's
        section ends after its answer)."""
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()

    def reset_sections(self) -> None:
        self.stack = []
        self.end_list_run()


_NUMERIC_CELL = re.compile(r"^[~≈<>]?\s*\$?\d[\d,.]*(?:\s*(?:[-–]|to)\s*\$?\d[\d,.]*)?\s*\+?$")


def table_row_text(cells: Sequence[str], header: Sequence[str] = ()) -> str:
    """A table row as one sentence. A bare number takes its column header as
    its unit ("| CPAP | 40 | 25 |" under "| Load | Watts | Hours |" reads
    "CPAP | 40 Watts | 25 Hours"), so a claim in a table is a claim."""
    out = []
    for i, c in enumerate(cells):
        c = c.strip()
        if not c:
            continue
        h = header[i].strip() if i < len(header) else ""
        if h and _NUMERIC_CELL.match(inline_text(c)) and len(h) <= 40:
            c = f"{c} {h}"
        out.append(c)
    return " | ".join(out)


def split_table_row(row: str) -> list[str]:
    """Cells of one GFM table row: outer pipes optional, `\\|` is a literal pipe."""
    s = (row or "").strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, cur, k = [], [], 0
    while k < len(s):
        ch = s[k]
        if ch == "\\" and k + 1 < len(s) and s[k + 1] == "|":
            cur.append("|")
            k += 2
            continue
        if ch == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        k += 1
    cells.append("".join(cur).strip())
    return cells


def _starts_block(line: str) -> bool:
    return bool(_FENCE.match(line) or _HEADING_LINE.match(line) or _QUOTE_LINE.match(line))


def _is_table_start(lines: Sequence[str], i: int) -> bool:
    if i + 1 >= len(lines):
        return False
    head, delim = lines[i], lines[i + 1]
    if not head.strip() or not _UNESCAPED_PIPE.search(head) or _starts_block(head):
        return False
    return bool(_TABLE_DELIM.match(delim)) and "|" in delim


def _is_table_body_row(line: str) -> bool:
    return bool(line.strip()) and bool(_UNESCAPED_PIPE.search(line)) and not _starts_block(line)


def markdown_blocks(text: str, *, field: str = "body", callouts: Mapping[str, Any] | None = None,
                    out: list[Block] | None = None) -> list[Block]:
    """Split markdown into Blocks: headings, paragraphs, list items (lazy
    continuation lines included), GFM table rows (outer pipes optional),
    blockquotes (safety callouts flagged) and code, the way the kit renderer
    reads them. Appends to `out` when given and returns it."""
    blocks: list[Block] = out if out is not None else []
    b = BlockBuilder(blocks, field, callouts or DEFAULT_CALLOUTS)
    lines = _HTML_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text or "").split("\n")
    para: list[str] = []
    para_line = 0
    item: list[str] = []
    item_line = 0
    item_ordered = False
    quote: list[tuple[int, str]] = []

    def flush_para():
        nonlocal para
        if para:
            b.add("paragraph", " ".join(para), para_line)
            para = []

    def flush_item():
        nonlocal item
        if item:
            b.add("list_item", " ".join(item), item_line, ordered=item_ordered)
            item = []

    def flush_quote():
        nonlocal quote
        if not quote:
            return
        first = next((t for _n, t in quote if t.strip()), "")
        total = sum(len(t) for _n, t in quote)
        is_callout = bool(first and b.label_re.search(inline_text(first))) and total <= b.max_chars
        chunk: list[str] = []
        start = quote[0][0]
        for n, t in quote + [(0, "")]:
            if t.strip():
                if not chunk:
                    start = n
                chunk.append(t.strip())
            elif chunk:
                b.add("quote", " ".join(chunk), start, callout=is_callout)
                chunk = []
        quote = []

    i = 0
    while i < len(lines):
        raw = lines[i]
        n = i + 1
        if _FENCE.match(raw):
            flush_para(); flush_item(); flush_quote(); b.end_list_run()
            body: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            b.add("code", "\n".join(body), n)
            i += 1                                   # the closing fence (or past the end)
            continue
        qm = _QUOTE_LINE.match(raw)
        if qm:
            flush_para(); flush_item()
            quote.append((n, qm.group(1)))
            i += 1
            continue
        flush_quote()
        if not raw.strip():
            flush_para(); flush_item()
            i += 1
            continue
        hm = _HEADING_LINE.match(raw)
        if hm:
            flush_para(); flush_item(); b.end_list_run()
            b.add("heading", hm.group(2), n, level=len(hm.group(1)))
            i += 1
            continue
        if _is_table_start(lines, i):
            flush_para(); flush_item(); b.end_list_run()
            header = split_table_row(raw)
            b.add("table_row", " | ".join(c for c in header if c), n)
            i += 2                                   # the header and the delimiter row
            while i < len(lines) and _is_table_body_row(lines[i]):
                b.add("table_row", table_row_text(split_table_row(lines[i]), header), i + 1)
                i += 1
            continue
        if _HR_LINE.match(raw):
            flush_para(); flush_item(); b.end_list_run()
            i += 1
            continue
        lm = _LIST_LINE.match(raw)
        if lm:
            flush_para(); flush_item()
            item, item_line, item_ordered = [lm.group(3)], n, lm.group(2) is None
            i += 1
            continue
        if item and not _RAW_HTML_LINE.match(raw):
            item.append(raw.strip())                 # a (lazy) continuation of the list item
            i += 1
            continue
        flush_item()
        if b.in_list_run and not para:
            b.end_list_run()
        if not para:
            para_line = n
        para.append(raw.strip())
        i += 1
    flush_para(); flush_item(); flush_quote()
    return blocks


def faq_entries(faqs: Any) -> list[tuple[str, str]]:
    """(question, answer) pairs from an editorial_articles.faqs value: a list
    of {"question","answer"} (or {"q","a"} / {"name","acceptedAnswer"})
    dicts, or its JSON text. Anything else is ignored."""
    if isinstance(faqs, (str, bytes)):
        try:
            faqs = _json.loads(faqs)
        except (ValueError, TypeError):
            return []
    out: list[tuple[str, str]] = []
    for f in faqs if isinstance(faqs, list) else []:
        if not isinstance(f, dict):
            continue
        q = f.get("question") or f.get("q") or f.get("name") or ""
        a = f.get("answer") or f.get("a") or f.get("acceptedAnswer") or ""
        if isinstance(a, dict):
            a = a.get("text") or ""
        if q or a:
            out.append((str(q), str(a)))
    return out


def article_blocks(*, body_md: str = "", title: str = "", subtitle: str = "", excerpt: str = "",
                   faqs: Any = None, callouts: Mapping[str, Any] | None = None) -> list[Block]:
    """Every field of an article as Blocks.

    The title is a level-1 heading (field "title") over the whole article:
    the subtitle, the excerpt, every body block and every FAQ entry list it
    among their ancestors. That is how the rendered page reads (the page
    chrome renders the title as the page's one <h1> and demotes an in-body
    '#' to <h2>; content_policy_audit maps that <h1> to a title-field
    heading), so a subject the title names carries into the article for
    rules that carry heading subjects: "Best Power Stations for CPAP
    Machines" over "The Delta 2 gives you about two days" is a CPAP runtime
    claim here exactly as it is on the live page. Rules that select the
    title field (a headline rule) still see only the title."""
    blocks: list[Block] = []
    top: tuple[int, ...] = ()
    t = inline_text(str(title or ""))
    if t:
        blocks.append(Block("title", "heading", t, 0, 1))
        top = (len(blocks) - 1,)
    for field, val in (("subtitle", subtitle), ("excerpt", excerpt)):
        t = inline_text(str(val or ""))
        if t:
            blocks.append(Block(field, "meta", t, 0, ancestors=top))
    start = len(blocks)
    markdown_blocks(body_md or "", field="body", callouts=callouts, out=blocks)
    if top:
        blocks[start:] = [dataclasses.replace(b, ancestors=top + b.ancestors) for b in blocks[start:]]
    for q, a in faq_entries(faqs):
        # The question is a level-7 heading over its answer, so a subject the
        # question names ("How long will it run a CPAP?") carries into the
        # answer ("About 20 hours.") for rules that carry heading subjects.
        anc = top
        if inline_text(q):
            blocks.append(Block("faqs", "heading", inline_text(q), 0, 7, ancestors=top))
            anc = top + (len(blocks) - 1,)
        for blk in markdown_blocks(a, field="faqs", callouts=callouts):
            blocks.append(dataclasses.replace(blk, ancestors=anc))
    return blocks


# ── Sentences and clauses ──────────────────────────────────────────────────

_ABBR = re.compile(r"\b(?:e\.g|i\.e|vs|etc|approx|Inc|Co|Ltd|Jr|Sr|Dr|Mr|Mrs|Ms|St|Fig|U\.S|U\.K|a\.m|p\.m"
                   r"|max|min|est|incl|ca|cf|al)\.$", re.I)
_SENT_END = re.compile(r"[.!?][\"'”’)\]*_]*\s+(?=[A-Z0-9\"'“‘(\[*_$❌⚠])")


def split_sentences(text: str) -> list[str]:
    """Sentences of one block (abbreviation-aware). Headings and table rows
    are one sentence each by construction."""
    out: list[str] = []
    start = 0
    t = text or ""
    for m in _SENT_END.finditer(t):
        if _ABBR.search(t[start:m.start() + 1]):
            continue
        s = t[start:m.end()].strip()
        if s:
            out.append(s)
        start = m.end()
    tail = t[start:].strip()
    if tail:
        out.append(tail)
    return out


def is_question(sentence: str) -> bool:
    return (sentence or "").rstrip(" \"'”’)*_").endswith("?")


def _question_exempts(sentence: str) -> bool:
    """A question is not an instruction, except a how-to question ("How do I
    backfeed my house?") or a rhetorical endorsement ("Why pay for an
    interlock when you can backfeed?")."""
    return is_question(sentence) and not _QUESTION_NOT_EXEMPT.search(sentence)


def _words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def _comma_is_boundary(sentence: str, seg_start: int, comma: int, lx: _Lexicon) -> bool:
    """Does the comma at `comma` end the clause that began at `seg_start`?"""
    before = sentence[seg_start:comma]
    if _CUE_ONLY_SEGMENT.match(before):
        return False                                  # "Never, ever ..." / "Do not, ..."
    if _LEADING_SUBORDINATE.match(before):
        return True                                   # "If you lack an inlet, backfeed ..."
    nxt = re.split(r",", sentence[comma + 1:], maxsplit=1)[0]
    if _CUE_ONLY_SEGMENT.match(nxt):
        return False                                  # "..., under any circumstances, ..."
    if _SUBJECT_START.match(nxt) or lx.imperative_start.match(nxt):
        return True
    if re.match(r"^\W*(?:just|simply|instead|rather|then|so|always|otherwise)\b", nxt, re.I):
        return True
    fv = _FINITE_VERB.search(nxt)
    # "live, exposed prongs THAT CAN kill you": the verb belongs to a
    # relative clause inside the list item, not to a new clause.
    return bool(fv) and not _RELATIVE.search(nxt[:fv.start()])


@functools.lru_cache(maxsize=4096)
def _bounds(sentence: str, lx: _Lexicon) -> tuple[tuple[int, int], ...]:
    s = sentence or ""
    # (start, end, kind): a separator is consumed; a contrastive word opens
    # the next clause; a comma is judged against the clause it would close,
    # so events are processed left to right.
    events = [(m.start(), m.end(), "hard") for m in _HARD_SEPARATOR.finditer(s)]
    events += [(m.start(), m.start(), "open") for m in _CONTRASTIVE.finditer(s)]
    events += [(m.start(), m.end(), "comma") for m in re.finditer(",", s)]
    events.sort(key=lambda e: (e[0], e[2] == "comma"))
    spans: list[tuple[int, int]] = []
    seg = 0
    for a, b, kind in events:
        if a < seg:
            continue                                   # inside a separator already consumed
        if kind == "comma" and not _comma_is_boundary(s, seg, a, lx):
            continue
        if a > seg:
            spans.append((seg, a))
        seg = a if kind == "open" else b
    spans.append((seg, len(s)))
    return tuple((a, b) for a, b in spans if s[a:b].strip()) or ((0, len(s)),)


def clause_bounds(sentence: str, lexicon: _Lexicon | None = None) -> list[tuple[int, int]]:
    """(start, end) spans of the clauses of `sentence`. Hard separators (; :
    em dash, spaced dashes, table pipes) and contrastive words always split;
    a comma splits only when a new clause starts after it (a subject, an
    imperative, a finite verb) or when it closes a leading subordinate
    clause; commas inside a list ("never use double-male cords, dryer
    adapters or ...") do not."""
    return list(_bounds(sentence or "", lexicon or _DEFAULT_LEXICON))


def _clause_of(bounds: Sequence[tuple[int, int]], pos: int) -> tuple[int, int]:
    for a, b in bounds:
        if a <= pos < b:
            return a, b
    return (bounds[-1] if bounds else (0, pos + 1))


def _next_clause(bounds: Sequence[tuple[int, int]], ce: int) -> tuple[int, int] | None:
    for a, b in bounds:
        if a >= ce:
            return a, b
    return None


def _masked(spans: list[tuple[int, int]], a: int, b: int) -> bool:
    return any(x < b and a < y for x, y in spans)


def _new_clause_after(text: str, lx: _Lexicon) -> bool:
    """Does `text` (what follows a comma or "and") start a new clause rather
    than continue a list?"""
    if lx.imperative_start.match(text):
        return True
    if re.match(r"^\W*[\w'’-]+", text) and _GAP_FINITE.match((_words(text) or [""])[0]):
        return True
    m = _SUBJECT_START.match(text)
    if m:
        rest = _words(text[m.end():])[:2]
        return any(_GAP_FINITE.match(w) or _FINITE_VERB.fullmatch(w) for w in rest)
    return False


def _cue_governs(s: str, cue_end: int, start: int, lx: _Lexicon, max_gap: int) -> bool:
    """Does the cue ending at `cue_end` govern the match at `start`? Only
    when nothing between them starts a new clause or predicate, and the
    match is at most `max_gap` words after the cue (a list "X, Y or Z" under
    one cue restarts the count at each item)."""
    gap = s[cue_end:start]
    dist = 0
    seg_start = cue_end
    pre_to: list[str] = []                    # words between the cue and an infinitive "to"
    for t in _GAP_TOKEN.finditer(gap):
        w = t.group(0).lower()
        pos = cue_end + t.end()
        if w == ",":
            seg = s[seg_start:cue_end + t.start()]
            seg_start = pos
            if not seg.strip() or _CUE_ONLY_SEGMENT.match(seg):
                continue                      # "Never, ever ..." / "Do not, under any circumstances, ..."
            if _new_clause_after(s[pos:], lx):
                return False
            dist, pre_to = 0, []              # a list: "never use X, Y or Z"
            continue
        if w in _SCOPE_BREAKERS:
            return False
        if w in ("or", "nor"):
            dist, pre_to = 0, []
            continue
        if w == "and":
            if _new_clause_after(s[pos:], lx):
                return False
            dist, pre_to = 0, []
            continue
        if w == "to" and any(x not in _CONTROL_WORDS for x in pre_to):
            return False                      # "Never pay an electrician TO backfeed": a purpose clause
        if dist > 0 and _GAP_FINITE.match(w):
            return False
        pre_to.append(w)
        dist += 1
        if dist > max_gap:
            return False
    return True


def _lifted(s: str, end: int, lx: _Lexicon) -> bool:
    """A condition right after the match ("... without shutting off the
    main", "..., unless you have an interlock") makes a prohibition or a
    hazard statement a conditional permission."""
    bounds = _bounds(s, lx)
    _cs, ce = _clause_of(bounds, max(end - 1, 0))
    if _LIFT.search(s[end:ce]):
        return True
    nxt = _next_clause(bounds, ce)
    return bool(nxt and _LIFT_START.match(s[nxt[0]:nxt[1]]))


_COORDINATOR = re.compile(r",|\b(?:and|or|then|plus)\b", re.I)


def _instruction_segments(c: str, lx: _Lexicon) -> list[str]:
    """The coordinated instructions of a clause that opens with one: "hire a
    licensed electrician | or just turn off the main first". A coordinator
    (",", "and", "or", "then") starts a new segment only when an instruction
    follows it; "a transfer switch or interlock kit" stays one."""
    cuts = [0]
    for m in _COORDINATOR.finditer(c):
        at = m.end() if m.group(0) == "," else m.start()
        nxt = c[at:]
        if m.start() > cuts[-1] and lx.instruction_start.match(nxt) and not _PROHIBITION_START.match(nxt):
            cuts.append(m.start())
    return [c[x:y] for x, y in zip(cuts, cuts[1:] + [len(c)])]


def _has_instruction(s: str, lx: _Lexicon) -> bool:
    """Does any clause of `s` give an instruction (an imperative, "so plug
    ...", "you should flip ...") that is not a prohibition and not a
    configured safe alternative ("hire a licensed electrician")? Each
    coordinated instruction is judged on its own: "hire a licensed
    electrician or just turn off the main first" gives one."""
    for a, b in _bounds(s, lx):
        c = s[a:b]
        if _PROHIBITION_START.match(c) or not lx.instruction_start.match(c):
            continue
        if all(any(p.search(seg) for p in lx.safe_instructions) for seg in _instruction_segments(c, lx)):
            continue
        return True
    return False


def _endorsing_tail(s: str, ce: int, lx: _Lexicon) -> bool:
    """A contrastive clause after the hazard that is not itself a warning
    ("Backfeeding is illegal, BUT it works") undoes the warning."""
    for a, b in _bounds(s, lx):
        if a < ce:
            continue
        c = s[a:b]
        if _CONTRAST_START.match(c) and not (lx.strong.search(c) or lx.hazard.search(c)):
            return True
    return False


def _has_prohibition(s: str, lx: _Lexicon) -> bool:
    """A prohibition cue anywhere in `s` that no idiom or denial masks."""
    masks = [(m.start(), m.end()) for rx in (lx.idioms, lx.denial) for m in rx.finditer(s)]
    return any(not _masked(masks, m.start(), m.end()) for m in lx.strong.finditer(s))


def negation_reason(sentence: str, start: int, end: int, lexicon: _Lexicon | None = None) -> str:
    """Why the match at sentence[start:end] is NOT an endorsement, or "".

      "prohibition:<cue>"   a prohibition governs it ("Never backfeed", "Do
                            not, under any circumstances, backfeed"): the cue
                            sits at most `max_gap_words` before the match, or
                            inside it, with no clause boundary, subordinator,
                            purpose "to", finite verb or new clause between
      "negation:<cue>"      a weak negation that governs it within 3 words
                            ("no double-male cords")
      "permission-negated"  "you don't have to backfeed"
      "hazard"              a hazard predicate follows it in the clause
                            ("backfeeding can electrocute a line worker") and
                            the sentence gives no instruction
      "cautionary-conditional"  "If someone backfeeds ..., X can be killed"
                            (the hazard is in the main clause; no instruction)
    Never negated: permission phrases ("you don't need an electrician to
    ..."), idioms ("never been easier", "never worry about ... again"),
    denials of danger ("it's not dangerous to ..."), and any of the above
    followed by a condition ("Never backfeed WITHOUT ...", "Backfeeding is
    illegal UNLESS ...")."""
    lx = lexicon or _DEFAULT_LEXICON
    s = sentence or ""
    bounds = _bounds(s, lx)
    cs, ce = _clause_of(bounds, start)
    clause_upto = s[cs:end]
    lifted = _lifted(s, end, lx)
    masks: list[tuple[int, int]] = []
    for rx in (lx.idioms, lx.denial):
        masks += [(cs + m.start(), cs + m.end()) for m in rx.finditer(clause_upto)]
    for m in lx.permission.finditer(clause_upto):
        a, b = cs + m.start(), cs + m.end()
        if a >= start:
            continue
        # "you don't have to backfeed" / "you don't need a double-male cord"
        # negate the practice; "you don't need an electrician to <X>" grants
        # permission for X, and "a station that never needs gas can <X>"
        # negates the gas, so their cues are masked instead.
        gap = [w.lower() for w in _words(s[b:start])]
        if len(gap) <= 2 and all(w in _PERMISSION_GAP_WORDS for w in gap):
            return "" if lifted else "permission-negated"
        masks.append((a, b))
    for m in lx.strong.finditer(clause_upto):
        a, b = cs + m.start(), cs + m.end()
        if _masked(masks, a, b) or a >= end:
            continue
        cue = m.group(0).lower()
        if a >= start:                                 # inside the match: "generators should never ..."
            return "" if lifted else f"prohibition:{cue}"
        # "A station that never needs gas can backfeed": the cue sits in a
        # relative clause and a modal starts the main predicate.
        if _RELATIVE.search(s[cs:a]) and _MODAL.search(s[b:start]):
            continue
        if _cue_governs(s, b, start, lx, lx.max_gap):
            return "" if lifted else f"prohibition:{cue}"
    for m in lx.weak.finditer(clause_upto):
        a, b = cs + m.start(), cs + m.end()
        if _masked(masks, a, b) or a >= start:
            continue
        if m.group(0).lower() == "without" and (_CANT_WITHOUT.search(s[cs:a]) or any(
                not _masked(masks, cs + x.start(), cs + x.end()) for x in lx.strong.finditer(s[cs:a]))):
            continue                                   # "you can't heat the house without backfeeding"
        if _cue_governs(s, b, start, lx, 3):
            return "" if lifted else f"negation:{m.group(0).lower()}"
    if lifted or _has_instruction(s, lx) or _endorsing_tail(s, ce, lx):
        return ""
    after = s[end:ce]
    hz = lx.hazard.search(after)
    if hz and not _masked([(end + x.start(), end + x.end()) for x in lx.denial.finditer(after)],
                          end + hz.start(), end + hz.end()):
        return "hazard"
    if _CONDITIONAL_OPENERS.match(s[cs:ce]):
        nxt = _next_clause(bounds, ce)
        if nxt and lx.hazard.search(s[nxt[0]:nxt[1]]):
            return "cautionary-conditional"
    return ""


def is_instruction(sentence: str, start: int, lexicon: _Lexicon | None = None) -> bool:
    """Does the clause holding sentence[start] open with an imperative
    ("Backfeed only with the main breaker off")?"""
    lx = lexicon or _DEFAULT_LEXICON
    cs, _ce = _clause_of(_bounds(sentence or "", lx), start)
    return bool(lx.imperative_start.match((sentence or "")[cs:]))


def _callout_mention_only(sentence: str, start: int, lx: _Lexicon) -> bool:
    """A callout exempts a sentence that only names a practice: no
    instruction in any clause ("If you must backfeed, turn off the main"),
    and no permission for the practice in its own clause ("Backfeeding is OK
    with the main off", "it's not dangerous to backfeed")."""
    if _has_instruction(sentence, lx):
        return False
    cs, ce = _clause_of(_bounds(sentence, lx), start)
    clause = sentence[cs:ce]
    return not (lx.denial.search(clause) or _PERMISSIVE.search(clause))


# ── Concessions: "Never X. If you must, <steps>." ──────────────────────────
# A follow-up that calls the conceded practice workable: the subject is a
# pronoun that refers back to it ("it works", "that's fine", "doing so is
# OK"), never another noun ("a power station is safe indoors") or a dummy
# "it" with an infinitive of its own ("it's fine to use a power station").
_CONCESSION_PERMISSIVE = re.compile(
    rf"\b(?:it|this|that|they|these|those|doing\s+(?:it|so|this|that))"
    rf"(?:{_APOS}s|{_APOS}re|{_APOS}ll(?:\s+be)?|\s+(?:is|are|was|were)"
    r"|\s+(?:will|should|can|could|would|may|might)(?:\s+be)?)?\s+"
    r"(?:(?:still|usually|generally|normally|mostly|often|perfectly|totally|completely|probably|actually"
    r"|really|technically|reasonably|relatively|pretty|fairly|just)\s+)?"
    r"(?:works?|worked|do(?:es)?\s+the\s+(?:job|trick)|gets?\s+the\s+job\s+done"
    r"|do(?=\s*(?:[,.;:!?—–]|$|\s+(?:in|for|if|when|until|as)\b))"
    r"|(?:fine|ok|okay|safe|safer|acceptable|alright|all\s+right|doable|manageable|an?\s+option)"
    r"(?!\s+to\s+(?!(?:do|try|run|use|keep|leave|operate)\s+(?:it|so|this|that|one|them)\b)))"
    r"(?![\w'’-])", re.I)
_IF_OPENER = re.compile(r"^\W*(?:(?:but|and|so|or|then|now|well|yet|ok|okay)\W+)?if\b", re.I)


def _concession_rest(sentence: str, m: re.Match, lx: _Lexicon) -> str:
    """What a concession says after its opener: after the whole conditional
    clause for an "if ..." opener ("If you must run it in the garage, KEEP
    THE DOOR OPEN"), else right after the opener ("Still, with the main
    off, IT WORKS"). An "if" clause with no boundary after it falls back to
    the text right after the opener."""
    s = sentence or ""
    if _IF_OPENER.match(m.group(0)):
        _cs, ce = _clause_of(_bounds(s, lx), max(m.end() - 1, 0))
        if s[ce:].strip(" \t,;:—–-.!?"):
            return s[ce:]
    return s[m.end():]


def concedes(sentence: str, lexicon: _Lexicon | None = None, followers: Sequence[str] = ()) -> bool:
    """Does `sentence` concede a practice the copy has just warned against?
    It must open with a concession opener ("If you must,", "In a pinch,",
    "Still,", "Here's how to do it") and then give an instruction (not a
    prohibition, not a configured safe alternative) or call the practice
    workable ("it works", "that's fine"). A sentence that introduces steps
    with ':' also counts the steps (`followers`, the texts of the list or
    section it introduces)."""
    lx = lexicon or _DEFAULT_LEXICON
    m = lx.concession.match(sentence or "")
    if not m:
        return False
    rest = _concession_rest(sentence, m, lx).lstrip(" \t,;:—–-")
    if rest and (_has_instruction(rest, lx) or _CONCESSION_PERMISSIVE.search(rest)):
        return True
    return any(_has_instruction(t, lx) for t in followers)


# ── Rules and notices ──────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Rule:
    id: str                       # "<rule_set>.<rule>"
    rule_set: str
    label: str
    action: str                   # reject | warn
    patterns: tuple[re.Pattern, ...]
    require_any: tuple[re.Pattern, ...] = ()
    # Exempting constructions BOUND to the match: an `unless` hit that
    # overlaps the match or touches it (a few glue words apart), e.g. "HAVE A
    # LICENSED ELECTRICIAN install a transfer switch". A mention elsewhere in
    # the sentence exempts nothing.
    unless: tuple[re.Pattern, ...] = ()
    # Exempting constructions anywhere in the match's own clause, e.g. the
    # passive agent "... a transfer switch or interlock INSTALLED BY A
    # LICENSED ELECTRICIAN". Like `unless`, a hit the copy negates ("instead
    # of having it installed by ...") exempts nothing.
    unless_in_clause: tuple[re.Pattern, ...] = ()
    negation: bool = True
    question_exempt: bool = True
    callout_exempt: bool = True
    # Headings (and FAQ questions) matching these carry the subject to their
    # section: "## CPAP" + "About 20 hours." is a CPAP runtime claim. Empty:
    # no carry.
    carry_subjects: tuple[re.Pattern, ...] = ()
    fields: tuple[str, ...] = ARTICLE_FIELDS
    why: str = ""
    fix: str = ""
    # "anchor": the pattern match is judged (negation, callouts, questions);
    # require_any only has to be present in the sentence. "claim": the
    # patterns name a SUBJECT and each require_any hit is a CLAIM about it;
    # the claim is judged, so "We don't publish CPAP runtimes" passes and
    # "Don't let an outage stop your CPAP: a station keeps it running for 2
    # nights" fails.
    negation_target: str = "anchor"
    # Claim rules: a subject named anywhere in a paragraph, list item, quote
    # or table row (by a carry_subjects pattern) makes every sentence of that
    # block a candidate ("Got a CPAP? This station runs it for 3 nights.").
    carry_block: bool = False
    # Anchor rules: a match the copy exempts (a prohibition, a hazard
    # statement, a callout, ...) followed within the lexicon's
    # concession_window sentences by a CONCESSION ("If you must, turn off the
    # main first.", "Still, with the main off, it works.") is a violation:
    # the follow-up tells the reader how to do the banned thing anyway.
    concession: bool = False
    # A sentence naming one of these subjects opens the same window even
    # with no match ("Gas generators belong outdoors."); its follow-up
    # counts only when it matches a concession claim ("In a pinch, THE
    # GARAGE WORKS"). Concession claims also count after an exempted match.
    concession_subjects: tuple[re.Pattern, ...] = ()
    concession_claims: tuple[re.Pattern, ...] = ()
    # Claims that count inside either window even when the sentence does not
    # open with a concession opener ("THE GARAGE WORKS if you leave the door
    # open.", "KEEP THE GARAGE DOOR WIDE OPEN if you run it there."). Keep
    # them narrow: unlike concession_claims nothing else gates them.
    concession_bare_claims: tuple[re.Pattern, ...] = ()


@dataclasses.dataclass(frozen=True)
class Notice:
    id: str                       # "<rule_set>.<notice>"
    rule_set: str
    label: str
    action: str
    triggers: tuple[re.Pattern, ...]
    notices: tuple[re.Pattern, ...]
    fields: tuple[str, ...] = ARTICLE_FIELDS
    message: str = ""
    suggested_text: str = ""


@dataclasses.dataclass(frozen=True)
class Violation:
    rule_id: str
    rule_set: str
    kind: str                     # "pattern" | "notice"
    action: str                   # reject | warn
    field: str
    excerpt: str
    matched: str = ""
    line: int = 0
    label: str = ""
    why: str = ""
    fix: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v not in ("", 0) or k in ("line",)}


@dataclasses.dataclass(frozen=True)
class Exemption:
    rule_id: str
    reason: str
    excerpt: str


ADDENDUM_MAX_EXAMPLES = 25


@dataclasses.dataclass
class ContentPolicyAudit:
    rule_sets: tuple[str, ...]
    violations: list[Violation]
    exemptions: list[Exemption] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def rejects(self) -> list[Violation]:
        return [v for v in self.violations if v.action == "reject"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.action == "warn"]

    @property
    def passes(self) -> bool:
        return not self.rejects

    def failure_reason(self, max_examples: int = 3) -> str:
        """One line: every rejecting rule with up to `max_examples` sentences."""
        if self.passes:
            return ""
        parts = []
        for rid, items in _group(self.rejects).items():
            ex = "; ".join(repr(v.excerpt[:120]) for v in items[:max_examples])
            what = items[0].label or rid
            parts.append(f"{rid} ({what}): {ex}" if items[0].kind == "pattern"
                         else f"{rid} (missing: {what}) — triggered by {ex}")
        return "CONTENT POLICY: " + " | ".join(parts)

    def addendum(self, max_examples: int = ADDENDUM_MAX_EXAMPLES) -> str:
        """The re-queue text for the author: every violation (rejects first,
        then warnings) with EVERY offending sentence (up to `max_examples`
        in all), why it is banned and how to fix it, so one rewrite can fix
        them all."""
        if not self.violations:
            return ""
        lines = [f"CONTENT POLICY — the article breaks the site's content policy "
                 f"(rule sets: {', '.join(self.rule_sets) or '-'}). Fix every item below in place; "
                 f"do not delete unrelated prose:"]
        n = 0
        budget = max(1, int(max_examples))
        for group in (self.rejects, self.warnings):
            for rid, items in _group(group).items():
                n += 1
                v = items[0]
                sev = "MUST FIX" if v.action == "reject" else "review"
                if v.kind == "notice":
                    lines.append(f"  {n}. [{rid} — {sev}] Missing {v.label or 'notice'}: the copy discusses it "
                                 f"here: {items[0].excerpt[:200]!r}. {v.why}".rstrip())
                    if v.fix:
                        lines.append(f"     Add, for example: \"{v.fix}\"")
                    continue
                lines.append(f"  {n}. [{rid} — {sev}] {v.label or rid} — {len(items)} sentence(s):")
                shown = items[:budget]
                budget = max(0, budget - len(shown))
                for x in shown:
                    where = x.field + (f" line {x.line}" if x.line else "")
                    lines.append(f"     - ({where}) {x.excerpt[:240]!r}")
                if len(items) > len(shown):
                    lines.append(f"     - ... and {len(items) - len(shown)} more sentence(s) of this kind; "
                                 f"run the self-check below to list them")
                if v.why:
                    lines.append(f"     Why: {v.why}")
                if v.fix:
                    lines.append(f"     Fix: {v.fix}")
        lines.append(
            "  If a flagged sentence is meant as a warning, phrase it as a prohibition that names the "
            "practice right after the cue (\"Never backfeed ...\", \"Do not use a double-male cord\") with "
            "no condition after it (\"never ... without / until / unless / if ...\" reads as a permission), "
            "or move it into a short safety callout: a blockquote whose first line starts with "
            "\"**Safety:**\". A callout may name a banned practice; it may never give steps or say a practice "
            "is safe. A warning sentence must not also give an instruction: put the safe alternative in its "
            "own sentence. Never follow a warning with a concession (\"If you must, ...\", \"In a pinch, "
            "...\", \"That said / Still / Even so, ...\", \"Here's how to do it safely:\") or with a workaround "
            "that needs no opener (\"The garage works if you leave the door open.\"): the sentences after a "
            "warning give only the safe alternative, with no such opener.")
        return "\n".join(lines)

    def summary(self) -> str:
        if not self.violations:
            return "content policy: pass"
        return (f"content policy: {len(self.rejects)} reject, {len(self.warnings)} warn ("
                + ", ".join(f"{rid}x{len(v)}" for rid, v in _group(self.violations).items()) + ")")

    def to_dict(self) -> dict:
        return {"rule_sets": list(self.rule_sets), "passes": self.passes,
                "rejects": [v.to_dict() for v in self.rejects],
                "warnings": [v.to_dict() for v in self.warnings],
                "exemptions": [dataclasses.asdict(e) for e in self.exemptions[:50]],
                "notes": list(self.notes)}


def _group(vs: Iterable[Violation]) -> dict[str, list[Violation]]:
    out: dict[str, list[Violation]] = {}
    for v in vs:
        out.setdefault(v.rule_id, []).append(v)
    return out


@dataclasses.dataclass(frozen=True)
class ContentPolicy:
    """The compiled rules a site opted into. `active` is False for a site
    with no rule sets: checks then pass trivially."""
    site_key: str
    rule_sets: tuple[str, ...]
    rules: tuple[Rule, ...]
    notices: tuple[Notice, ...]
    callouts: Mapping[str, Any]
    lexicon: _Lexicon = _DEFAULT_LEXICON

    @property
    def active(self) -> bool:
        return bool(self.rules or self.notices)


def _sentence_cache(blocks: Sequence[Block]) -> list[list[str]]:
    return [[blk.text] if blk.kind in ("heading", "table_row", "title", "code") else split_sentences(blk.text)
            for blk in blocks]


_REFERS_BACK = re.compile(r"\b(?:it|its|them|they|one|this|that|these|those)\b", re.I)
_GLUE_GAP = re.compile(r"\s*(?:(?:for|to|with|do|handle|perform|the|a|an|your|any|this|that|it|them|all|of|on|in)"
                       r"\s+){0,3}", re.I)


def _bound(u: re.Match, m: re.Match, s: str) -> bool:
    """Is the `unless` hit `u` bound to the match `m`: overlapping it, or
    touching it with at most three glue words between?"""
    if u.start() < m.end() and m.start() < u.end():
        return True
    gap = s[u.end():m.start()] if u.end() <= m.start() else s[m.end():u.start()]
    return bool(_GLUE_GAP.fullmatch(gap + " "))


def _unless_counts(s: str, u: re.Match, lx: _Lexicon) -> bool:
    """An exempting construction counts unless the copy negates it ("don't
    have a licensed electrician install it", "instead of having it installed
    by a licensed electrician"). A construction that is itself a prohibition
    ("never install ... without a licensed electrician") counts."""
    if lx.strong.match(s, u.start()):
        return True
    return not negation_reason(s, u.start(), u.end(), lx).startswith(("prohibition", "negation", "permission"))


def _unless_state(rule: Rule, s: str, m: re.Match, lx: _Lexicon) -> str:
    """"exempt" when a bound exempting construction states the safe path,
    "denied" when the only such construction is negated ("DON'T have a
    licensed electrician install it": the copy rejects the safe path, so no
    other exemption may rescue the match), else ""."""
    hits = [u for p in rule.unless for u in p.finditer(s) if _bound(u, m, s)]
    if rule.unless_in_clause:
        cs, ce = _clause_of(_bounds(s, lx), m.start())
        hits += [u for p in rule.unless_in_clause for u in p.finditer(s) if cs <= u.start() < ce]
    if not hits:
        return ""
    return "exempt" if any(_unless_counts(s, u, lx) for u in hits) else "denied"


def _exemption(rule: Rule, sentence: str, m: re.Match, blk: Block, lx: _Lexicon) -> str:
    if rule.callout_exempt and blk.callout and _callout_mention_only(sentence, m.start(), lx):
        return "callout"
    if rule.negation and blk.negated_lead and not _lifted(sentence, m.end(), lx):
        return "negated-lead-in"
    if rule.question_exempt and _question_exempts(sentence):
        return "question"
    if rule.negation:
        return negation_reason(sentence, m.start(), m.end(), lx)
    return ""


def _note_exemption(exemptions: list[Exemption], rule: Rule, why: str, s: str) -> None:
    if len(exemptions) < 200:
        exemptions.append(Exemption(rule.id, why, s[:200]))


def _rule_hits(rule: Rule, blocks: Sequence[Block], sents: list[list[str]], lx: _Lexicon,
               exemptions: list[Exemption]) -> list[Violation]:
    claim_mode = rule.negation_target == "claim" and bool(rule.require_any)
    # A heading or FAQ question naming the subject carries it to its section,
    # negated or not ("## Don't trust CPAP runtime charts" + "About 20 hours").
    subjects: set[int] = set()
    if rule.carry_subjects:
        for i, blk in enumerate(blocks):
            if blk.kind == "heading" and blk.field in rule.fields and any(
                    p.search(s) for s in sents[i] for p in rule.carry_subjects):
                subjects.add(i)
    out: list[Violation] = []
    # Sentences that open a concession window: (block, sentence) -> ("match"
    # | "subject", the sentence). A sentence already reported never does.
    opened: dict[tuple[int, int], tuple[str, str]] = {}
    violated: set[tuple[int, int]] = set()
    prev_subject = False                       # the previous list item named the subject
    for i, blk in enumerate(blocks):
        if blk.field not in rule.fields:
            prev_subject = False
            continue
        carried = bool(subjects) and any(a in subjects for a in blk.ancestors)
        own_subject = bool(rule.carry_block and rule.carry_subjects and blk.kind != "heading" and any(
            p.search(s) for s in sents[i] for p in rule.carry_subjects))
        if not carried and rule.carry_block:
            # "- Got a CPAP?" / "- A 1,000 Wh station runs it for 2 nights.":
            # the next item of the same list refers back with a pronoun.
            carried = own_subject or (prev_subject and blk.kind == "list_item"
                                      and bool(_REFERS_BACK.search(blk.text)))
        prev_subject = blk.kind == "list_item" and (own_subject or (prev_subject and carried))
        for j, s in enumerate(sents[i]):
            anchors = sorted((m for p in rule.patterns for m in p.finditer(s)), key=lambda m: m.start())
            if claim_mode:
                if not anchors and not carried:
                    continue
                targets = sorted((m for r in rule.require_any for m in r.finditer(s)), key=lambda m: m.start())
            else:
                if anchors and rule.require_any and not any(r.search(s) for r in rule.require_any):
                    anchors = []
                if not anchors and carried and rule.require_any:
                    anchors = sorted((m for r in rule.require_any for m in r.finditer(s)),
                                     key=lambda m: m.start())
                targets = anchors
            exempted = False
            for m in targets:
                state = _unless_state(rule, s, m, lx) if (rule.unless or rule.unless_in_clause) else ""
                if state == "exempt":
                    _note_exemption(exemptions, rule, "unless", s)
                    exempted = True
                    continue
                why = "" if state == "denied" else _exemption(rule, s, m, blk, lx)
                if why:
                    _note_exemption(exemptions, rule, why, s)
                    exempted = True
                    continue
                out.append(Violation(rule.id, rule.rule_set, "pattern", rule.action, blk.field, s[:240],
                                     m.group(0)[:80], blk.line, rule.label, rule.why, rule.fix))
                violated.add((i, j))
                break
            if rule.concession and (i, j) not in violated:
                if exempted:
                    opened[(i, j)] = ("match", s)
                elif rule.concession_subjects and any(p.search(s) for p in rule.concession_subjects):
                    opened[(i, j)] = ("subject", s)
    if opened:
        out += _concession_hits(rule, blocks, sents, lx, opened, violated)
    return out


# A concession window never crosses into these: code, tables and page / head
# metadata are not a follow-up to the sentence before them.
_WINDOW_STOP_KINDS = frozenset({"code", "table_row", "title", "meta"})
_MAX_SECTION_FOLLOWERS = 12


def _followers(blocks: Sequence[Block], i: int, *, heading: bool) -> list[str]:
    """The steps a concession introduces. After a heading ("## If you must"):
    the text blocks of its section. After a sentence ending with ':': the
    list (or table) right after it, or else the next paragraph or quote."""
    out: list[str] = []
    field, level = blocks[i].field, blocks[i].level
    run: str | None = None                     # the kind of the run a ':' introduces
    for blk in blocks[i + 1:i + 1 + _MAX_SECTION_FOLLOWERS]:
        if blk.field != field or blk.kind in ("title", "meta"):
            break
        if heading:
            if blk.kind == "heading" and blk.level <= level:
                break
            if blk.kind not in ("heading", "code"):
                out.append(blk.text)
            continue
        if run is None:
            if blk.kind in ("paragraph", "quote"):
                out.append(blk.text)
                break
            if blk.kind not in ("list_item", "table_row"):
                break
            run = blk.kind
        if blk.kind != run:
            break
        out.append(blk.text)
    return out


def _concession_claim(rule: Rule, sentence: str, lx: _Lexicon, *, bare: bool = False) -> re.Match | None:
    """A concession claim of `rule` (its concession_bare_claims when `bare`)
    the sentence makes, and does not itself negate or warn about: "never run
    it in the garage" is no claim."""
    for p in (rule.concession_bare_claims if bare else rule.concession_claims):
        for cm in p.finditer(sentence):
            if not negation_reason(sentence, cm.start(), cm.end(), lx):
                return cm
    return None


def _concession_hits(rule: Rule, blocks: Sequence[Block], sents: list[list[str]], lx: _Lexicon,
                     opened: Mapping[tuple[int, int], tuple[str, str]],
                     violated: set[tuple[int, int]]) -> list[Violation]:
    """Violations for concessions: within `lx.concession_window` sentences
    after a sentence that opened a window (same field, never across a
    heading, a table or code), a sentence that opens with a concession
    opener and concedes the practice. After an exempted match ("Never
    backfeed through a dryer outlet."), an instruction or "it works" is
    enough ("If you absolutely must, turn off the main first."); after a
    bare subject ("Gas generators belong outdoors.") only a configured
    concession claim counts ("In a pinch, the garage works"). Either way a
    concession_bare_claims match counts with no opener ("The garage works
    if you leave the door open.")."""
    if lx.concession_window <= 0:
        return []
    units = [(i, j) for i, blk in enumerate(blocks) if blk.field in rule.fields for j in range(len(sents[i]))]
    pos = {u: k for k, u in enumerate(units)}
    out: list[Violation] = []
    flagged: set[tuple[int, int]] = set()
    for u in sorted(opened, key=lambda x: pos.get(x, -1)):
        k = pos.get(u)
        if k is None:
            continue
        how, opening = opened[u]
        field = blocks[u[0]].field
        for fi, fj in units[k + 1:k + 1 + lx.concession_window]:
            fb = blocks[fi]
            if fb.field != field or fb.kind in _WINDOW_STOP_KINDS:
                break
            s = sents[fi][fj]
            bm = (_concession_claim(rule, s, lx, bare=True)
                  if rule.concession_bare_claims and (fi, fj) not in flagged and (fi, fj) not in violated else None)
            if bm:
                flagged.add((fi, fj))
                out.append(Violation(rule.id, rule.rule_set, "pattern", rule.action, fb.field,
                                     f"{opening} {s}"[:240], bm.group(0).strip(" \t,;:—–")[:80], fb.line,
                                     rule.label, rule.why, rule.fix))
                break
            m = lx.concession.match(s)
            if m and (fi, fj) not in flagged and (fi, fj) not in violated:
                if fb.kind == "heading":
                    followers = _followers(blocks, fi, heading=True)
                elif fj == len(sents[fi]) - 1 and s.rstrip(" \t*_").endswith(":"):
                    followers = _followers(blocks, fi, heading=False)
                else:
                    followers = []
                if _concession_claim(rule, s, lx) or (how == "match" and concedes(s, lx, followers)):
                    flagged.add((fi, fj))
                    out.append(Violation(rule.id, rule.rule_set, "pattern", rule.action, fb.field,
                                         f"{opening} {s}"[:240], m.group(0).strip()[:80], fb.line, rule.label,
                                         rule.why, rule.fix))
                    break
            if fb.kind == "heading":
                break                                  # a new section: the window ends here
    return out


# A notice phrase does not count when the copy negates IT: "you don't need a
# licensed electrician", "no licensed electrician required", "a licensed
# electrician isn't needed", "skip the electrician". "Never ... without a
# licensed electrician" is a requirement, so it counts.
_NOTICE_NEGATED_BEFORE = re.compile(
    rf"(?:\b(?:don{_APOS}t|do\s+not|doesn{_APOS}t|does\s+not|won{_APOS}t|will\s+not|never|not)\s+(?:even\s+|really\s+)?"
    r"(?:need|needs|require|requires|have\s+to\s+(?:hire|call|pay|use|get))\s+(?:to\s+(?:hire|call|pay|use|get|involve)\s+)?"
    r"|\bno\s+need\s+(?:for|to\s+(?:hire|call|pay|get|involve))\s+"
    r"|\b(?:skip(?:ping)?|instead\s+of|rather\s+than|save\s+(?:on|the\s+cost\s+of)|without\s+the\s+(?:cost|expense)\s+of)\s+"
    r"(?:(?:hiring|calling|paying(?:\s+for)?)\s+)?"
    r"|\bno\s+)(?:an?\s+|any\s+|the\s+|your\s+)?$", re.I)
_NOTICE_NEGATED_AFTER = re.compile(
    rf"^\s*(?:(?:is|are)\s+not|isn{_APOS}t|aren{_APOS}t|not)\s+(?:really\s+)?(?:needed|required|necessary)\b", re.I)
_NOTICE_WITHOUT = re.compile(
    r"\bwithout\s+(?:first\s+)?(?:hiring\s+|calling\s+|paying(?:\s+for)?\s+|using\s+|needing\s+|involving\s+)?"
    r"(?:an?\s+|any\s+|the\s+|your\s+)?$", re.I)


def _notice_counts(s: str, start: int, end: int, lx: _Lexicon) -> bool:
    """Does the notice phrase at s[start:end] state the requirement (rather
    than deny it)?"""
    cs, ce = _clause_of(_bounds(s, lx), start)
    before, after = s[cs:start], s[end:ce]
    if _NOTICE_NEGATED_BEFORE.search(before) or _NOTICE_NEGATED_AFTER.match(after):
        return False
    if _NOTICE_WITHOUT.search(before):
        # "Never connect a station to your wiring without a licensed
        # electrician" requires one; "you can do it without a licensed
        # electrician" denies the need.
        return _has_prohibition(s, lx)
    return True


def _notice_hits(notice: Notice, blocks: Sequence[Block], sents: list[list[str]],
                 lx: _Lexicon) -> list[Violation]:
    trigger: tuple[str, str, Block] | None = None
    satisfied = False
    for i, blk in enumerate(blocks):
        if blk.field not in notice.fields:
            continue
        for s in sents[i]:
            if trigger is None:
                for t in notice.triggers:
                    m = t.search(s)
                    if m:
                        trigger = (s, m.group(0), blk)
                        break
            if not satisfied:
                for p in notice.notices:
                    if any(_notice_counts(s, m.start(), m.end(), lx) for m in p.finditer(s)):
                        satisfied = True
                        break
        if satisfied and trigger is not None:
            break
    if trigger is None or satisfied:
        return []
    s, matched, blk = trigger
    return [Violation(notice.id, notice.rule_set, "notice", notice.action, blk.field, s[:240], matched[:80],
                      blk.line, notice.label, notice.message, notice.suggested_text)]


def check_blocks(policy: ContentPolicy, blocks: Sequence[Block], *, url: str = "",
                 notes: Iterable[str] = ()) -> ContentPolicyAudit:
    """Run every rule and notice of `policy` over `blocks`."""
    audit = ContentPolicyAudit(policy.rule_sets, [], [], list(notes))
    if not policy.active:
        return audit
    sents = _sentence_cache(blocks)
    for rule in policy.rules:
        audit.violations += _rule_hits(rule, blocks, sents, policy.lexicon, audit.exemptions)
    for notice in policy.notices:
        audit.violations += _notice_hits(notice, blocks, sents, policy.lexicon)
    if url:
        audit.violations = [dataclasses.replace(v, url=url) for v in audit.violations]
    return audit


SKIP_MARKER_RULE = "content-policy.skip-marker"


def _skip_marker_violations(policy: ContentPolicy, fields: Iterable[tuple[str, str]]) -> list[Violation]:
    """Authored copy may never carry the rendered-page skip marker: it would
    hide the text from the live and kit-gate scans."""
    try:
        rx = re.compile(str(policy.callouts.get("html_skip_re") or DEFAULT_CALLOUTS["html_skip_re"]), re.I)
    except re.error:
        return []
    out = []
    for field, text in fields:
        m = rx.search(text or "")
        if m:
            out.append(Violation(
                SKIP_MARKER_RULE, "content-policy", "pattern", "reject", field,
                (text or "")[max(0, m.start() - 60):m.end() + 60].replace("\n", " "), m.group(0), 0,
                "a content-policy skip marker in authored copy",
                "The marker hides text from the rendered-page content-policy scans; only templates may mark "
                "third-party data with it.",
                "Remove the data-content-policy attribute (and any raw HTML wrapper added for it)."))
    return out


def check_article(policy: ContentPolicy, *, body_md: str = "", title: str = "", subtitle: str = "",
                  excerpt: str = "", faqs: Any = None) -> ContentPolicyAudit:
    """Audit one article (markdown body plus its metadata and FAQ entries)."""
    if not policy.active:
        return ContentPolicyAudit(policy.rule_sets, [])
    blocks = article_blocks(body_md=body_md, title=title, subtitle=subtitle, excerpt=excerpt, faqs=faqs,
                            callouts=policy.callouts)
    audit = check_blocks(policy, blocks, notes=callout_notes(blocks, policy))
    raw = [("body", body_md)] + [("faqs", f"{q}\n{a}") for q, a in faq_entries(faqs)]
    audit.violations = _skip_marker_violations(policy, raw) + audit.violations
    return audit


def check_markdown(policy: ContentPolicy, text: str, *, field: str = "body") -> ContentPolicyAudit:
    """Audit one markdown document as a single field (default "body")."""
    blocks = markdown_blocks(text, field=field, callouts=policy.callouts)
    audit = check_blocks(policy, blocks, notes=callout_notes(blocks, policy))
    if policy.active:
        audit.violations = _skip_marker_violations(policy, [(field, text)]) + audit.violations
    return audit


def callout_notes(blocks: Sequence[Block], policy: ContentPolicy) -> list[str]:
    """A note for every quote that is labelled like a safety callout but was
    too long to count as one (its sentences were checked as plain text)."""
    try:
        rx = re.compile(str(policy.callouts.get("markdown_label_re") or ""), re.I)
    except re.error:
        return []
    seen: set[int] = set()
    out = []
    for blk in blocks:
        if blk.kind == "quote" and not blk.callout and blk.line not in seen and rx.search(blk.text):
            seen.add(blk.line)
            out.append(f"{blk.field} line {blk.line}: a quote labelled as a safety callout is longer than "
                       f"{policy.callouts.get('max_chars')} characters, so it was checked as plain text")
    return out


# ── Config ─────────────────────────────────────────────────────────────────

def merge_config(base: Mapping[str, Any] | None, over: Mapping[str, Any] | None) -> dict:
    """Lay `over` (the storage copy, or an explicit file) on `base`.

    Top-level keys replace, except: `rule_sets` merges per rule set (a rule
    set named in `over` REPLACES that whole set), `sites` merges per site
    (keys update; a nested `publish_gate` merges key by key), and
    `default`, `callouts` and `negation` merge key by key."""
    out = copy.deepcopy(dict(base or {}))
    for k, v in (over or {}).items():
        if k == "rule_sets" and isinstance(v, dict):
            rs = dict(out.get("rule_sets") or {})
            rs.update(copy.deepcopy(v))
            out["rule_sets"] = rs
        elif k == "sites" and isinstance(v, dict):
            sites = {sk: dict(sv or {}) for sk, sv in (out.get("sites") or {}).items()}
            for sk, sv in v.items():
                cur = sites.get(sk, {})
                sv = dict(sv or {})
                if isinstance(cur.get("publish_gate"), dict) and isinstance(sv.get("publish_gate"), dict):
                    sv["publish_gate"] = {**cur["publish_gate"], **sv["publish_gate"]}
                sites[sk] = {**cur, **copy.deepcopy(sv)}
            out["sites"] = sites
        elif k in ("default", "callouts", "negation") and isinstance(v, dict):
            cur = dict(out.get(k) or {})
            for kk, vv in v.items():
                if isinstance(vv, dict) and isinstance(cur.get(kk), dict):
                    cur[kk] = {**cur[kk], **copy.deepcopy(vv)}
                else:
                    cur[kk] = copy.deepcopy(vv)
            out[k] = cur
        else:
            out[k] = copy.deepcopy(v)
    return out


def read_storage_copy(storage) -> dict | None:
    """The storage copy of the config, None when storage holds none.

    Raises ContentPolicyConfigError when a copy exists but cannot be read or
    parsed, or when storage cannot be reached: an opted-in site must never
    silently fall back to the repo default (which opts nobody in) during a
    storage outage."""
    try:
        raw = storage.read_bytes(CONFIG_KEY)
        if raw is None:
            # Some backends answer None for an unreachable store as well as
            # for a missing key; a listing tells them apart (it raises when
            # the store is unreachable).
            listed = storage.list_prefix(CONFIG_KEY, limit=5)
            if CONFIG_KEY not in listed:
                return None
            uncached = getattr(storage, "read_bytes_uncached", None)
            raw = uncached(CONFIG_KEY) if callable(uncached) else None
            if raw is None:
                raise ContentPolicyConfigError(f"storage {CONFIG_KEY}: listed but unreadable")
    except ContentPolicyConfigError:
        raise
    except Exception as e:  # noqa: BLE001 - any storage failure fails closed
        raise ContentPolicyConfigError(f"storage {CONFIG_KEY}: unreadable ({type(e).__name__}: "
                                       f"{str(e)[:160]})") from None
    try:
        stored = _json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ContentPolicyConfigError(f"storage {CONFIG_KEY}: not valid JSON ({e})") from None
    if stored in (None, {}):
        return None
    if not isinstance(stored, dict):
        raise ContentPolicyConfigError(f"storage {CONFIG_KEY}: expected a JSON object, got {type(stored).__name__}")
    return stored


def load_config(storage=None, path: str | _pathlib.Path | None = None) -> dict:
    """The repo default, the storage copy laid over it (when `storage` holds
    one), then an explicit JSON file. A missing repo default leaves an empty
    config, which opts no site in (fail closed: nothing is promoted). A
    storage copy that exists but cannot be read, or an unreachable store,
    raises ContentPolicyConfigError."""
    cfg: dict = {}
    try:
        cfg = _json.loads(_REPO_CONFIG.read_text())
    except Exception as e:  # noqa: BLE001 - reported, never fatal
        _sys.stderr.write(f"[content-policy] repo default unreadable ({e}); no rule sets\n")
    if storage is not None:
        stored = read_storage_copy(storage)
        if stored:
            cfg = merge_config(cfg, stored)
    if path:
        try:
            cfg = merge_config(cfg, _json.loads(_pathlib.Path(path).read_text()))
        except Exception as e:  # noqa: BLE001
            raise ContentPolicyConfigError(f"--config {path}: {e}") from None
    return cfg


def site_block(cfg: Mapping[str, Any], site_hint: str) -> tuple[str, dict]:
    """(site key, block) for `site_hint`: the longest `sites` key contained in
    the hint (an agent id, a site name or a URL), else ("", {})."""
    hint = (site_hint or "").lower()
    if not hint:
        return "", {}
    for key in sorted((cfg.get("sites") or {}), key=len, reverse=True):
        if key and key.lower() in hint:
            return key, dict((cfg.get("sites") or {}).get(key) or {})
    return "", {}


def _as_list(v: Any, where: str) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return list(v)
    raise ContentPolicyConfigError(f"{where}: expected a list, got {type(v).__name__}")


def _fields(v: Any, where: str) -> tuple[str, ...]:
    fs = tuple(str(x) for x in _as_list(v, where)) or ARTICLE_FIELDS
    bad = [f for f in fs if f not in ARTICLE_FIELDS]
    if bad:
        raise ContentPolicyConfigError(f"{where}: unknown field(s) {bad} (allowed: {ARTICLE_FIELDS})")
    return fs


def _action(v: Any, where: str, default: str = "reject") -> str:
    a = str(v or default).lower()
    if a not in ACTIONS:
        raise ContentPolicyConfigError(f"{where}: action must be one of {ACTIONS}, got {v!r}")
    return a


def compile_rule_set(name: str, spec: Mapping[str, Any],
                     overrides: Mapping[str, str] | None = None) -> tuple[list[Rule], list[Notice]]:
    """Rules and notices of one rule set. `overrides` maps "<set>.<id>" (or
    "<set>") to an action; "off" drops it."""
    overrides = overrides or {}
    if not isinstance(spec, Mapping):
        raise ContentPolicyConfigError(f"rule_sets.{name}: expected an object")
    set_override = overrides.get(name)
    rules: list[Rule] = []
    seen: set[str] = set()
    for i, r in enumerate(_as_list(spec.get("rules"), f"rule_sets.{name}.rules")):
        where = f"rule_sets.{name}.rules[{i}]"
        if not isinstance(r, Mapping) or not r.get("id"):
            raise ContentPolicyConfigError(f"{where}: every rule needs an id")
        rid = f"{name}.{r['id']}"
        if rid in seen:
            raise ContentPolicyConfigError(f"{where}: duplicate id {rid}")
        seen.add(rid)
        action = _action(overrides.get(rid) or set_override or r.get("action"), where)
        pats = tuple(_compile(p, f"{where}.patterns") for p in _as_list(r.get("patterns"), where))
        if not pats:
            raise ContentPolicyConfigError(f"{where}: no patterns")
        carry = r.get("carry_heading_subject", False)
        if carry is True:
            carry_pats = pats
        elif carry in (False, None):
            carry_pats = ()
        else:
            carry_pats = tuple(_compile(p, f"{where}.carry_heading_subject")
                               for p in _as_list(carry, f"{where}.carry_heading_subject"))
        target = str(r.get("negation_target") or "anchor")
        if target not in NEGATION_TARGETS:
            raise ContentPolicyConfigError(f"{where}.negation_target: one of {NEGATION_TARGETS}, got {target!r}")
        require_any = tuple(_compile(p, f"{where}.require_any") for p in _as_list(r.get("require_any"), where))
        if target == "claim" and not require_any:
            raise ContentPolicyConfigError(f"{where}: negation_target 'claim' needs require_any (the claims)")
        concession_subjects = tuple(_compile(p, f"{where}.concession_subjects")
                                    for p in _as_list(r.get("concession_subjects"), f"{where}.concession_subjects"))
        concession_claims = tuple(_compile(p, f"{where}.concession_claims")
                                  for p in _as_list(r.get("concession_claims"), f"{where}.concession_claims"))
        concession_bare_claims = tuple(
            _compile(p, f"{where}.concession_bare_claims")
            for p in _as_list(r.get("concession_bare_claims"), f"{where}.concession_bare_claims"))
        if concession_subjects and not (concession_claims or concession_bare_claims):
            raise ContentPolicyConfigError(f"{where}: concession_subjects needs concession_claims or "
                                           f"concession_bare_claims (what a concession after a bare subject must "
                                           f"say to count)")
        # Claim rules carry their subject through the block and the section
        # instead, so a concession is checked for anchor rules by default.
        concession = r.get("concession", target == "anchor")
        if not isinstance(concession, bool):
            raise ContentPolicyConfigError(f"{where}.concession: expected true or false, got {concession!r}")
        # Bare claims count only inside a concession window. A claim rule has
        # none unless it says so; an explicit "concession": false is an
        # operator's opt-out and turns them off with the rest.
        if concession_bare_claims and "concession" not in r and not concession:
            raise ContentPolicyConfigError(f"{where}: concession_bare_claims needs concession: true (they count "
                                           f"only inside a concession window, which a claim rule has none of by "
                                           f"default)")
        if action == "off":
            continue
        rules.append(Rule(
            id=rid, rule_set=name, label=str(r.get("label") or r["id"]), action=action, patterns=pats,
            require_any=require_any,
            unless=tuple(_compile(p, f"{where}.unless") for p in _as_list(r.get("unless"), where)),
            unless_in_clause=tuple(_compile(p, f"{where}.unless_in_clause")
                                   for p in _as_list(r.get("unless_in_clause"), where)),
            negation=bool(r.get("negation", True)), question_exempt=bool(r.get("question_exempt", True)),
            callout_exempt=bool(r.get("callout_exempt", True)), carry_subjects=carry_pats,
            fields=_fields(r.get("fields"), f"{where}.fields"),
            why=str(r.get("why") or ""), fix=str(r.get("fix") or ""), negation_target=target,
            carry_block=bool(r.get("carry_block_subject", bool(carry_pats) and target == "claim")),
            concession=concession, concession_subjects=concession_subjects,
            concession_claims=concession_claims, concession_bare_claims=concession_bare_claims))
    notices: list[Notice] = []
    for i, n in enumerate(_as_list(spec.get("required_notices"), f"rule_sets.{name}.required_notices")):
        where = f"rule_sets.{name}.required_notices[{i}]"
        if not isinstance(n, Mapping) or not n.get("id"):
            raise ContentPolicyConfigError(f"{where}: every notice needs an id")
        nid = f"{name}.{n['id']}"
        if nid in seen:
            raise ContentPolicyConfigError(f"{where}: duplicate id {nid}")
        seen.add(nid)
        action = _action(overrides.get(nid) or set_override or n.get("action"), where)
        triggers = tuple(_compile(p, f"{where}.triggers") for p in _as_list(n.get("triggers"), where))
        texts = tuple(_compile(p, f"{where}.notice_patterns") for p in _as_list(n.get("notice_patterns"), where))
        if not triggers or not texts:
            raise ContentPolicyConfigError(f"{where}: needs triggers and notice_patterns")
        if action == "off":
            continue
        notices.append(Notice(
            id=nid, rule_set=name, label=str(n.get("label") or n["id"]), action=action, triggers=triggers,
            notices=texts, fields=_fields(n.get("fields"), f"{where}.fields"),
            message=str(n.get("message") or ""), suggested_text=str(n.get("suggested_text") or "")))
    return rules, notices


def resolve_policy(site_hint: str = "", *, storage=None, config: Mapping[str, Any] | None = None,
                   rule_sets: Sequence[str] | None = None) -> ContentPolicy:
    """The compiled policy for `site_hint`.

    Rule sets: `rule_sets` when given (an explicit list, e.g. a kit gate's
    --rule-set flags), else the site block's `rule_sets` (which REPLACES the
    default block's list), else the default block's (empty in the repo
    default: nobody is opted in). Overrides (`rule_overrides`,
    {"<set>.<id>" | "<set>": action}) come from the default block, then the
    site block. The negation vocabulary is the generic default, the top-level
    `negation` block, then each selected rule set's own `negation` block.
    Raises ContentPolicyConfigError for an unknown rule set, a config that
    does not compile, or an unreadable storage copy."""
    cfg = dict(config) if config is not None else load_config(storage)
    default = dict(cfg.get("default") or {})
    key, site = site_block(cfg, site_hint)
    if rule_sets is not None:
        names = [str(n) for n in rule_sets]
    elif "rule_sets" in site:
        names = [str(n) for n in _as_list(site.get("rule_sets"), f"sites.{key}.rule_sets")]
    else:
        names = [str(n) for n in _as_list(default.get("rule_sets"), "default.rule_sets")]
    overrides: dict[str, str] = {}
    for blk, where in ((default, "default"), (site, f"sites.{key}")):
        ov = blk.get("rule_overrides") or {}
        if not isinstance(ov, Mapping):
            raise ContentPolicyConfigError(f"{where}.rule_overrides: expected an object")
        overrides.update({str(k): _action(v, f"{where}.rule_overrides.{k}") for k, v in ov.items()})
    defined = cfg.get("rule_sets") or {}
    rules: list[Rule] = []
    notices: list[Notice] = []
    vocab: list[tuple[str, Mapping[str, Any]]] = []
    for n in dict.fromkeys(names):
        if n not in defined:
            raise ContentPolicyConfigError(
                f"rule set {n!r} is not defined (known: {sorted(defined) or 'none'})")
        r, nt = compile_rule_set(n, defined[n], overrides)
        rules += r
        notices += nt
        if isinstance(defined[n], Mapping) and defined[n].get("negation"):
            if not isinstance(defined[n]["negation"], Mapping):
                raise ContentPolicyConfigError(f"rule_sets.{n}.negation: expected an object")
            vocab.append((n, defined[n]["negation"]))
    callouts = {**DEFAULT_CALLOUTS, **dict(cfg.get("callouts") or {})}
    for k in ("markdown_label_re", "html_class_re", "html_attr_re", "html_skip_re"):
        _compile(callouts[k], f"callouts.{k}")
    if not isinstance(callouts.get("jsonld_skip_types"), (list, tuple)):
        raise ContentPolicyConfigError("callouts.jsonld_skip_types: expected a list of schema.org types")
    neg = cfg.get("negation") or {}
    if not isinstance(neg, Mapping):
        raise ContentPolicyConfigError("negation: expected an object")
    lexicon = _Lexicon.build(neg, rule_set_vocab=vocab) if (neg or vocab) else _DEFAULT_LEXICON
    return ContentPolicy(site_key=key, rule_sets=tuple(dict.fromkeys(names)), rules=tuple(rules),
                         notices=tuple(notices), callouts=callouts, lexicon=lexicon)


def defined_rule_sets(config: Mapping[str, Any] | None = None, storage=None) -> list[str]:
    cfg = dict(config) if config is not None else load_config(storage)
    return sorted((cfg.get("rule_sets") or {}).keys())
