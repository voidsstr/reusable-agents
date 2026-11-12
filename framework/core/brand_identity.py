"""Brand / manufacturer identity: is a disagreement between two maker names
a real contradiction, or a normal catalog pattern?

WHY THIS EXISTS
---------------
Marketplace listings carry a `brand` and a `manufacturer` field, and the two
routinely disagree for reasons that say nothing about the product:

  parent company      a WD_BLACK SSD whose manufacturer is "Sandisk
                      Technologies, Inc." (WD spun its flash business off as
                      SanDisk); a 3.5" diskette box branded Imation with 3M
                      copy (Imation came out of 3M's storage business)
  product line        "Logitech G" vs "Logitech"; a "Logitech for Creators"
                      Blue Yeti described as a "Logitech G Blue Yeti"
  legal entity        "ORICO" vs "SHENZHEN ORICO TECHNOLOGIES CO";
                      "Seagate" vs "Seagate Bare Drives"
  reseller program    "Amazon Renewed" as the brand of a refurbished dock
  platform brand      an "Intel Pentium 4" sold by a reseller, an "NVIDIA
                      GeForce" card built by a board partner
  private label       a "Cylewet" switch made by OEM "Qianxin"

On 2026-10-16..11-12 a catalog audit treated every one of these as "the
listing cannot be trusted" and deactivated 82 products, 25 of them linked from
published articles. This module is the one place that decides how two maker
names relate, so every catalog check reads the same answer.

RELATIONS (`relation(brand, manufacturer, title=...)`)
------------------------------------------------------
  unknown    the manufacturer is missing or a placeholder ("Unknown", "N/A")
  reseller   either name is a reseller / refurb / no-brand marker
  same       the same maker after normalisation (case, punctuation, legal
             suffixes, city/province prefixes, a line suffix like " G")
  alias      different names in one configured brand group (parent company,
             former name, product line)
  partner    the brand is a platform/chip brand that partner-built listings
             lead with (NVIDIA, AMD, Intel...)
  differs    none of the above: a real disagreement a person may look at

Only `differs` is a contradiction, and even that is about attribution, not
about whether the listing shows the right product.

CONFIG
------
Everything is data, in three layers:

  1. DEFAULT_CONFIG (this file)
  2. storage `config/brand-identity-config.json`, shared by every consumer
     (`load_shared()`), so one alias list serves every catalog check
  3. a caller's own block, for example the catalog audit's
     `image_name_rule.brand_identity` in `config/catalog-audit-config.json`

`resolve(overrides, shared=load_shared())` merges them. A list key in a
later layer REPLACES the list. `extra_*` keys APPEND to their base list, and
they accumulate across layers, so a caller's extra groups add to the shared
ones instead of dropping them:

    {"extra_brand_groups": [["nexigo", "nexight"]],
     "extra_reseller_patterns": ["\\\\bopen box\\\\b"]}

Names are matched after `normalize()`, so groups are written lowercase and
without punctuation or legal suffixes.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Optional

RELATIONS = ("unknown", "reseller", "same", "alias", "partner", "differs")
#: relations under which a brand/manufacturer disagreement is explained
EXPLAINED = frozenset({"unknown", "reseller", "same", "alias", "partner"})

DEFAULT_CONFIG: dict = {
    # Parent company / former name / product line. A name may sit in more
    # than one group (HyperX: Kingston memory, HP peripherals).
    "brand_groups": [
        ["western digital", "wd", "wd black", "wd_black", "wdblack", "wd blue",
         "wd red", "wd green", "wd purple", "wd gold", "wd elements", "my passport",
         "hgst", "g technology", "sandisk", "sandisk professional", "sandisk optimus"],
        ["seagate", "lacie", "maxtor"],
        ["toshiba", "kioxia", "ocz"],
        ["micron", "crucial", "ballistix"],
        ["kingston", "kingston fury", "hyperx"],
        ["hp", "hewlett packard", "hpe", "hewlett packard enterprise", "omen",
         "hyperx", "poly", "plantronics"],
        ["dell", "alienware", "dell emc", "emc"],
        ["lenovo", "legion", "thinkpad", "thinkcentre", "motorola"],
        ["asus", "asustek", "rog", "republic of gamers", "tuf gaming"],
        ["msi", "micro star", "micro star international"],
        ["gigabyte", "giga byte", "aorus"],
        ["corsair", "elgato", "scuf", "scuf gaming", "origin pc"],
        ["logitech", "logitech g", "logitech for creators", "logitech creators",
         "blue microphones", "blue yeti", "astro gaming", "ultimate ears", "jaybird",
         "saitek", "streamlabs"],
        ["amd", "advanced micro devices", "ati", "radeon", "xilinx"],
        ["nvidia", "geforce", "quadro", "mellanox"],
        ["apple", "beats", "beats by dr dre"],
        ["samsung", "samsung semiconductor"],
        ["sony", "playstation", "sony interactive entertainment"],
        ["microsoft", "xbox"],
        ["imation", "3m"],
        ["anker", "anker innovations", "soundcore", "eufy", "nebula", "ankermake"],
        ["tp link", "tplink", "kasa", "tapo", "deco", "mercusys", "omada"],
        ["netgear", "arlo", "orbi", "nighthawk"],
        ["belkin", "wemo"],
        ["turtle beach", "roccat"],
        ["lexar", "longsys"],
        ["zotac", "pc partner"],
        ["powercolor", "tul"],
        ["xfx", "pine technology"],
        ["acer", "predator"],
        ["be quiet", "listan"],
        ["ubiquiti", "unifi", "ubnt"],
    ],
    "extra_brand_groups": [],
    # Regexes (search) on the normalised name: a reseller, refurb or no-brand
    # marker says nothing about who made the product.
    "reseller_patterns": [
        r"\brenewed\b", r"\brefurbish", r"\bamazon warehouse\b", r"\bgeneric\b",
        r"\bunbranded\b", r"\bno ?brand\b", r"\bnon ?brand\b", r"^oem", r"\boem\b",
        r"\bwholesale\b", r"\bliquidat", r"\bsurplus\b", r"\bpre ?owned\b",
    ],
    "extra_reseller_patterns": [],
    # A name equal to one of these (after normalisation) is no name at all.
    "unknown_values": [
        "", "unknown", "unknown brand", "n a", "na", "none", "null", "nil", "other",
        "others", "not specified", "unspecified", "not applicable", "does not apply",
        "brand", "manufacturer", "no", "0",
    ],
    "extra_unknown_values": [],
    # Chip / platform brands that partner-built or resold listings lead with.
    "platform_brands": ["nvidia", "geforce", "quadro", "amd", "radeon", "ati", "intel",
                        "qualcomm", "snapdragon", "mediatek"],
    "extra_platform_brands": [],
    # Dropped from names before comparing: legal suffixes, filler words and
    # the city/province prefixes common in OEM entity names.
    "stop_tokens": [
        "the", "and", "of", "for", "by", "co", "inc", "incorporated", "llc", "ltd",
        "limited", "corp", "corporation", "company", "group", "gmbh", "ag", "sa", "srl",
        "bv", "plc", "pty", "kk", "lp", "technology", "technologies", "tech",
        "electronics", "electronic", "electric", "industrial", "industry", "industries",
        "international", "intl", "global", "trading", "trade", "store", "shop", "official",
        "direct", "factory", "manufacturing", "manufacturer", "products", "product",
        "enterprise", "enterprises", "holdings", "systems", "solutions", "usa", "us",
        "america", "american", "shenzhen", "guangzhou", "dongguan", "zhuhai", "ningbo",
        "hangzhou", "xiamen", "yiwu", "shanghai", "beijing", "hongkong", "hk", "zhongshan",
        "foshan", "huizhou", "suzhou", "wenzhou", "jinhua", "fuzhou", "quanzhou", "taizhou",
        "changsha", "wuhan", "chengdu", "qingdao", "tianjin", "jiangmen", "jiangsu",
        "zhejiang", "guangdong", "fujian", "china", "taiwan", "taipei",
    ],
    "extra_stop_tokens": [],
}

_LIST_KEYS = ("brand_groups", "reseller_patterns", "unknown_values", "platform_brands",
              "stop_tokens")

CONFIG_KEY = "config/brand-identity-config.json"


def load_shared(storage=None) -> dict:
    """The shared storage layer (CONFIG_KEY), or {} when it is absent or
    unreadable (the defaults still apply)."""
    try:
        if storage is None:
            from framework.core.storage import get_storage
            storage = get_storage()
        v = storage.read_json(CONFIG_KEY)
    except Exception:  # noqa: BLE001 -- a missing shared layer is not an error
        return {}
    return dict(v) if isinstance(v, Mapping) else {}


def resolve(overrides: Optional[Mapping[str, Any]] = None, *,
            shared: Optional[Mapping[str, Any]] = None) -> dict:
    """DEFAULT_CONFIG <- `shared` (the storage layer) <- `overrides`. A list
    key in a later layer replaces the list; `extra_<key>` lists append to it
    and accumulate across layers. Unknown keys are kept, so a caller can
    store its own knobs in the same block. An already-resolved config is
    returned as it is."""
    if isinstance(overrides, Mapping) and "_compiled" in overrides and not shared:
        return overrides  # type: ignore[return-value]
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_CONFIG.items()}
    extras: dict[str, list] = {k: list(DEFAULT_CONFIG.get("extra_" + k) or []) for k in _LIST_KEYS}
    for layer in (shared, overrides):
        for k, v in (layer or {}).items() if isinstance(layer, Mapping) else []:
            if k == "_compiled":
                continue
            if k.startswith("extra_") and k[6:] in _LIST_KEYS:
                if isinstance(v, (list, tuple)):
                    extras[k[6:]] += [x for x in v if x not in extras[k[6:]]]
                continue
            out[k] = list(v) if isinstance(v, (list, tuple)) else v
    for k in _LIST_KEYS:
        out["extra_" + k] = extras[k]
        if extras[k]:
            out[k] = list(out.get(k) or []) + list(extras[k])
    out["_compiled"] = _compile(out)
    return out


def _cfg(cfg: Optional[Mapping]) -> dict:
    if isinstance(cfg, Mapping) and "_compiled" in cfg:
        return cfg  # type: ignore[return-value]
    return resolve(cfg)


def _fold(s: Any) -> str:
    """Lowercase ASCII-ish text: NFKD-fold (strips accents and the bold-math
    letters some listings use), drop format characters (U+200E and friends),
    and turn every non-alphanumeric run into one space."""
    t = unicodedata.normalize("NFKD", str(s or ""))
    kept = []
    for ch in t:
        cat = unicodedata.category(ch)
        if cat[0] == "M" or cat == "Cf":      # accents, U+200E and other format chars
            continue
        kept.append(" " if cat[0] in ("C", "Z") else ch)  # control / separators split words
    t = "".join(kept).lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _compile(cfg: Mapping) -> dict:
    stop = {_fold(t) for t in cfg.get("stop_tokens") or []}
    groups: list[tuple[str, ...]] = []
    for g in cfg.get("brand_groups") or []:
        names = tuple(sorted({_fold(a) for a in g if _fold(a)}, key=len, reverse=True))
        if names:
            groups.append(names)
    pats = []
    for p in cfg.get("reseller_patterns") or []:
        try:
            pats.append(re.compile(p))
        except re.error:
            continue
    return {
        "stop": stop,
        "groups": groups,
        "reseller": pats,
        "unknown": {_fold(v) for v in cfg.get("unknown_values") or []},
        "platform": {_fold(v) for v in cfg.get("platform_brands") or [] if _fold(v)},
    }


def normalize(name: Any, cfg: Optional[Mapping] = None) -> str:
    """Folded name without stop tokens ("SHENZHEN ORICO TECHNOLOGIES CO" ->
    "orico"). Falls back to the folded name when every token is a stop token."""
    c = _cfg(cfg)["_compiled"]
    folded = _fold(name)
    toks = [t for t in folded.split() if t not in c["stop"]]
    return " ".join(toks) if toks else folded


def _has_phrase(haystack: str, phrase: str) -> bool:
    """Whole-token match of `phrase` inside `haystack` (both folded)."""
    if not phrase or not haystack:
        return False
    return re.search(r"(?:^| )" + re.escape(phrase) + r"(?: |$)", haystack) is not None


def is_unknown(name: Any, cfg: Optional[Mapping] = None) -> bool:
    c = _cfg(cfg)["_compiled"]
    return _fold(name) in c["unknown"]


def is_reseller(name: Any, cfg: Optional[Mapping] = None) -> bool:
    c = _cfg(cfg)["_compiled"]
    folded = _fold(name)
    return bool(folded) and any(p.search(folded) for p in c["reseller"])


def is_platform(name: Any, cfg: Optional[Mapping] = None) -> bool:
    c = _cfg(cfg)["_compiled"]
    folded = _fold(name)
    return any(_has_phrase(folded, p) for p in c["platform"])


def groups_for(name: Any, cfg: Optional[Mapping] = None) -> set[int]:
    """Indices of the brand groups `name` belongs to: the name equals a group
    member, contains one as whole tokens ("western digital technologies"), or
    squashes to one ("wdblack")."""
    c = _cfg(cfg)["_compiled"]
    folded = _fold(name)
    norm = normalize(name, cfg)
    if not folded:
        return set()
    squashed = {folded.replace(" ", ""), norm.replace(" ", "")}
    out = set()
    for i, names in enumerate(c["groups"]):
        for a in names:
            if (_has_phrase(folded, a) or _has_phrase(norm, a)
                    or a.replace(" ", "") in squashed):
                out.add(i)
                break
    return out


def same_maker(a: Any, b: Any, cfg: Optional[Mapping] = None) -> bool:
    """Same maker after normalisation: equal when squashed, one squashed name a
    prefix of the other (>= 3 chars: "seagate" / "seagate bare drives",
    "logitech" / "logitech g"), or one name's tokens all inside the other's
    ("tessan" / "zhuhai tessan power")."""
    na, nb = normalize(a, cfg), normalize(b, cfg)
    if not na or not nb:
        return False
    sa, sb = na.replace(" ", ""), nb.replace(" ", "")
    if sa == sb:
        return True
    short, long_ = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(short) >= 3 and long_.startswith(short):
        return True
    ta = {t for t in na.split() if len(t) >= 2}
    tb = {t for t in nb.split() if len(t) >= 2}
    return bool(ta and tb and (ta <= tb or tb <= ta))


def _title_brand(title: Any, cfg: Optional[Mapping]) -> str:
    """The brand a title leads with, when its first word is a known brand
    (group member or platform brand); else ""."""
    folded = _fold(title)
    if not folded:
        return ""
    c = _cfg(cfg)["_compiled"]
    for size in (3, 2, 1):
        head = " ".join(folded.split()[:size])
        if not head:
            continue
        if head in c["platform"] or any(head in names for names in c["groups"]):
            return head
    return ""


def relation(brand: Any, manufacturer: Any, *, title: Any = "",
             cfg: Optional[Mapping] = None) -> str:
    """How the listing's brand relates to its manufacturer. One of RELATIONS.

    With no usable brand the title stands in for it: a title that names the
    manufacturer (or a name in its brand group) is `same` / `alias`; a title
    that leads with a different known brand is compared like a brand; any
    other title is `unknown` (it claims no maker we can name)."""
    c = _cfg(cfg)
    if is_unknown(manufacturer, c):
        return "unknown"
    if is_reseller(manufacturer, c) or is_reseller(brand, c):
        return "reseller"
    b = "" if is_unknown(brand, c) else brand
    if not b:
        folded_title = _fold(title)
        man = normalize(manufacturer, c)
        if man and (_has_phrase(folded_title, man) or same_maker(man, _title_brand(title, c), c)):
            return "same"
        mg = groups_for(manufacturer, c)
        if mg and any(_has_phrase(folded_title, a) for i in mg for a in c["_compiled"]["groups"][i]):
            return "alias"
        b = _title_brand(title, c)
        if not b:
            return "unknown"
    if same_maker(b, manufacturer, c):
        return "same"
    if groups_for(b, c) & groups_for(manufacturer, c):
        return "alias"
    if is_platform(b, c):
        return "partner"
    return "differs"


def makers_in_text(text: Any, cfg: Optional[Mapping] = None) -> dict[int, set[str]]:
    """{brand group index: {surface names found}} for free text such as an
    auditor's reason. Whole-token matches only, longest names first, and a
    name inside an already-matched longer name is not counted twice."""
    c = _cfg(cfg)["_compiled"]
    folded = _fold(text)
    out: dict[int, set[str]] = {}
    if not folded:
        return out
    taken: list[tuple[int, int]] = []
    names = sorted({(a, i) for i, g in enumerate(c["groups"]) for a in g},
                   key=lambda x: len(x[0]), reverse=True)
    for a, i in names:
        for m in re.finditer(r"(?:^| )(" + re.escape(a) + r")(?= |$)", folded):
            s, e = m.span(1)
            if any(s < te and e > ts for ts, te in taken):
                continue
            taken.append((s, e))
            out.setdefault(i, set()).add(a)
    return out


def describe(brand: Any, manufacturer: Any, *, title: Any = "",
             cfg: Optional[Mapping] = None) -> dict:
    """relation() plus the inputs that produced it, for audit artifacts."""
    c = _cfg(cfg)
    return {
        "brand": brand or "", "manufacturer": manufacturer or "",
        "relation": relation(brand, manufacturer, title=title, cfg=c),
        "brand_groups": sorted(groups_for(brand, c)) if brand else [],
        "manufacturer_groups": sorted(groups_for(manufacturer, c)) if manufacturer else [],
    }


__all__ = [
    "CONFIG_KEY", "DEFAULT_CONFIG", "EXPLAINED", "RELATIONS", "describe", "groups_for",
    "is_platform", "is_reseller", "is_unknown", "load_shared", "makers_in_text", "normalize",
    "relation", "resolve", "same_maker",
]
