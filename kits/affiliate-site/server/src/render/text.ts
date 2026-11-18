/**
 * Plain-text helpers shared by the SEO, JSON-LD and sitemap helpers: word-cut
 * truncation that never ends on a dangling word, and the length a string
 * takes once HTML-escaped.
 *
 * Ported from SpecPicks src/services/ssrHead.ts (see
 * unit/render/fixtures/ported-from-render.json): truncate (here truncateAtWord),
 * TRAILING_FUNCTION_WORDS, TRAILING_TITLE_JUNK, stripDanglingTail,
 * dropDanglingModifier, dropQuestionStub, dropUnclosedParen, htmlLen (here
 * htmlLength) and the Product-name trim. What changed: SpecPicks' hardware
 * "model family with its number cut off" rule (RTX, Ryzen 7, ...) is niche
 * vocabulary, so it is an option (`partialTail`) a site may pass, not a
 * built-in; and a cut that lands exactly at the end of a word keeps that word
 * (SpecPicks backed up to the previous space and lost it).
 */

/**
 * Short function words (articles, prepositions, conjunctions, copulas,
 * interrogatives, pronouns). A word cut that lands right after one reads as
 * an unfinished fragment in a SERP ("... Emulator Into an | Brand"), so
 * truncateAtWord drops the trailing run and ends on a content word.
 */
export const TRAILING_FUNCTION_WORDS: ReadonlySet<string> = new Set([
  'a', 'an', 'the',
  'of', 'in', 'on', 'at', 'to', 'by', 'for', 'with', 'into', 'from',
  'as', 'is', 'are', 'was', 'were', 'be',
  'and', 'or', 'but', 'nor',
  'vs', 'via', 'over', 'under', 'up', 'down', 'out',
  // A prenominal modifier whose noun was cut off ("... for Local").
  'local',
  // Interrogatives and the auxiliaries that open a question clause.
  'can', 'what', 'how', 'why', 'who', 'whom', 'whose', 'which',
  'when', 'where', 'does', 'do', 'did', 'will', 'would', 'should',
  'could', 'has', 'have', 'had', 'am',
  // Pronouns and determiners ("...: Can You" is still a stub).
  'i', 'you', 'it', 'we', 'they', 'he', 'she', 'me', 'us', 'them', 'him', 'her',
  'my', 'your', 'our', 'their', 'its', 'this', 'that', 'these', 'those',
]);

/**
 * Punctuation and conjunction glyphs that never end a finished title: a cut
 * ending on '&', '+', '/', '×' or a bare '|' reads as "list continues".
 */
export const TRAILING_TITLE_JUNK = /[\s,;:.!?\-—–&+/×|]+$/u;

/** Collapse runs of whitespace to one space and trim. */
export function collapseWhitespace(s: string | null | undefined): string {
  return String(s ?? '').replace(/\s+/g, ' ').trim();
}

/**
 * The length `s` takes once HTML-escaped (& < > " ' each grow). An SEO
 * crawler measures the rendered <title>, so budgets are checked on this.
 */
export function htmlLength(s: string): number {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .length;
}

export interface TruncateOptions {
  /**
   * A niche-specific "family name whose model number was cut off" tail, e.g.
   * /\s+(?:RTX|Ryzen\s+[3579])$/i. Dropped before the function-word pass so a
   * cut never ends naming no product. Default: none.
   */
  partialTail?: RegExp;
}

/**
 * Cut `s` to at most `n` characters at a word boundary and append '…'.
 *
 * The cut lands on the last space when that keeps at least 75% of the
 * budget (else a hard cut), then drops trailing punctuation and dangling
 * function words, so the head ends on a content word. Text that already fits
 * is returned collapsed and unchanged.
 */
export function truncateAtWord(s: string | null | undefined, n: number, opts: TruncateOptions = {}): string {
  if (!s) return '';
  const clean = s.replace(/\s+/g, ' ').trim();
  if (clean.length <= n) return clean;
  const hard = clean.slice(0, Math.max(0, n - 1));
  const lastSpace = hard.lastIndexOf(' ');
  // A hard cut that already ends a word (the next character is a space or
  // closing punctuation) keeps that word; SpecPicks dropped it.
  const endsWord = /[\s,;:.!?)]/.test(clean.charAt(hard.length));
  let cut = endsWord ? hard : lastSpace >= Math.floor((n - 1) * 0.75) ? hard.slice(0, lastSpace) : hard;
  cut = cut.replace(TRAILING_TITLE_JUNK, '');
  for (;;) {
    if (opts.partialTail) {
      const partial = cut.replace(opts.partialTail, '');
      if (partial !== cut && partial) { cut = partial.replace(TRAILING_TITLE_JUNK, ''); continue; }
    }
    const m = cut.match(/^(.*?)(\s+)([A-Za-z']+)$/);
    if (!m) break;
    if (!TRAILING_FUNCTION_WORDS.has((m[3] as string).toLowerCase())) break;
    cut = (m[1] as string).replace(TRAILING_TITLE_JUNK, '');
    if (!cut) break;
  }
  cut = cut.replace(TRAILING_TITLE_JUNK, '');
  return `${cut}…`;
}

/**
 * Trim to at most `n` characters at a word boundary WITHOUT an ellipsis.
 * Structured-data names ending in '…' read as truncated, low-quality content
 * to Google, so JSON-LD names are cut clean instead.
 */
export function trimNoEllipsis(s: string | null | undefined, n: number): string {
  const clean = collapseWhitespace(s);
  if (clean.length <= n) return clean;
  const hard = clean.slice(0, n);
  const lastSpace = hard.lastIndexOf(' ');
  return (lastSpace >= Math.floor(n * 0.75) ? hard.slice(0, lastSpace) : hard)
    .replace(/[\s,;:.!?\-—–…]+$/u, '');
}

/**
 * A word cut can land inside a parenthetical ("... Costs (12"). When the head
 * opens more '(' than it closes, drop the unclosed tail, unless that leaves
 * fewer than 20 characters.
 */
export function dropUnclosedParen(s: string): string {
  const open = (s.match(/\(/g) || []).length;
  const close = (s.match(/\)/g) || []).length;
  if (open <= close) return s;
  const cut = s.lastIndexOf('(');
  const head = s.slice(0, cut).replace(TRAILING_TITLE_JUNK, '').trim();
  return head.length >= 20 ? head : s;
}

/** Strip a dangling connector, punctuation or unclosed parenthetical left by a cut. */
export function stripDanglingTail(s: string): string {
  return dropUnclosedParen((s || '').replace(TRAILING_TITLE_JUNK, ''));
}

/**
 * A cut ending "... for CPU-Only" kept the modifier and lost what it
 * modifies. When the cut ends on a preposition plus one word that the full
 * text continues past, end before the preposition (if 20+ chars remain).
 */
export function dropDanglingModifier(cut: string, full: string): string {
  if (!cut || collapseWhitespace(full) === cut) return cut;
  const m = cut.match(/^(.*\S)\s+(?:for|with|on|in|under|of)\s+\S+$/i);
  return m && (m[1] as string).length >= 20 ? (m[1] as string).replace(TRAILING_TITLE_JUNK, '') : cut;
}

/**
 * A cut that stops inside a question ("...: Which Surface", "...: Can a
 * 128GB") is an unfinished question, however many of its words fit. End at
 * the separator before the question instead (if 20+ chars remain).
 */
export function dropQuestionStub(cut: string, full: string): string {
  if (!cut || collapseWhitespace(full) === cut) return cut;
  const m = cut.match(/^(.*\S)\s*(?::|\?|\s[—–-])\s+(?:which|what|how|why|is|are|does|do|can|should|when|where|who)(?:\s+[^\s:?—–]+)*$/i);
  return m && (m[1] as string).length >= 20 ? (m[1] as string).replace(TRAILING_TITLE_JUNK, '') : cut;
}

/**
 * A plain-text value (a name or title from the database) ready for JSON-LD
 * or escaping: the common HTML entities that leak into stored text decoded,
 * whitespace collapsed. Nothing that looks like a tag is removed: "Cable
 * <30 ft>" is a name, not markup.
 */
export function plainText(s: string | null | undefined): string {
  return collapseWhitespace(String(s ?? '')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&(lt|gt|quot|#39|apos);/gi, (_m, e: string) => {
      switch (e.toLowerCase()) {
        case 'lt': return '<';
        case 'gt': return '>';
        case 'quot': return '"';
        default: return "'";
      }
    })
    .replace(/&amp;/gi, '&'));
}

/**
 * Visible text of an HTML fragment: tags removed, the common entities
 * decoded, whitespace collapsed. For meta descriptions and JSON-LD text built
 * from rendered snippets; not a sanitizer.
 */
export function htmlToText(html: string | null | undefined): string {
  return collapseWhitespace(String(html ?? '')
    .replace(/<(script|style)\b[\s\S]*?<\/\1\s*>/gi, ' ')
    .replace(/<!--[\s\S]*?-->/g, ' ')
    // Block boundaries become spaces; inline tags (<b>, <a>, <em>) vanish without one.
    .replace(/<\/?(?:p|br|div|li|ul|ol|dl|dt|dd|h[1-6]|tr|td|th|table|thead|tbody|section|article|aside|header|footer|nav|blockquote|figure|figcaption|details|summary|hr)\b[^>]*>/gi, ' ')
    .replace(/<[^>]*>/g, '')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&(lt|gt|quot|#39|apos);/gi, (_m, e: string) => {
      switch (e.toLowerCase()) {
        case 'lt': return '<';
        case 'gt': return '>';
        case 'quot': return '"';
        default: return "'";
      }
    })
    .replace(/&amp;/gi, '&'));
}
