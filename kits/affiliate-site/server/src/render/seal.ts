/**
 * Seal an HTML fragment so nothing in it can reach past its own end.
 *
 * The page shell renders the 2(k) notice and the earnings disclosure after
 * the content (in the footer), so malformed content must never swallow or
 * hide what follows it. A browser carries parsing state across a fragment's
 * end in several ways (each confirmed in headless Chromium):
 *   - an unterminated comment, CDATA section, bogus comment ("<!x", "<?x") or
 *     tag (an attribute quote left open) runs on into the page chrome;
 *   - a raw-text or RCDATA element (script, style, textarea, title, xmp,
 *     iframe, noembed, noframes, noscript) reads everything up to its own end
 *     tag as text, and plaintext never ends. A script's "<!--<script>" run
 *     even skips a "</script>" that looks like its end;
 *   - </main> cannot close template, select, table, object, marquee or
 *     applet, so the chrome would land inside them (inert, or not rendered);
 *   - a formatting element (a, b, font, nobr, ...) left open is re-created,
 *     attributes and all, around every later run of text: an unclosed
 *     <b hidden> hides the footer, an unclosed <a> turns it into a link;
 *   - a stray <body ...> or <html ...> start tag merges its attributes into
 *     the document's own element (<body hidden> hides the whole page).
 *
 * sealHtmlFragment() neutralises each. A construct that cannot be closed
 * sensibly (comment, CDATA, raw text, an unterminated or document-level tag)
 * has its '<' escaped, so it reads as text and swallows nothing after it; an
 * element that is merely left open gets its end tag appended. Well-formed
 * input comes back unchanged, and the output is a fixed point.
 *
 * Inside svg and math (foreign content) no element is raw text and CDATA ends
 * only at "]]>". The model stays in foreign content until the svg or math
 * element closes (it does not follow HTML breakout tags or integration
 * points), which can only make it escape more than a browser needs, never
 * less. "<![CDATA[" is held to "]]>" everywhere for the same reason.
 *
 * This is a structural guard, not a sanitizer: it keeps markup from leaking
 * out of the fragment; it does not remove scripts, handlers or styles from
 * trusted content.
 */

const WHITESPACE = new Set(['\t', '\n', '\f', '\r', ' ']);

/** Elements whose content the tokenizer reads as text up to their own end tag. */
const RAW_TEXT = new Set(['script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript']);
/** Formatting elements the parser re-creates around later text while they stay open. */
const FORMATTING = new Set(['a', 'b', 'big', 'code', 'em', 'font', 'i', 'nobr', 's', 'small', 'strike', 'strong', 'tt', 'u']);
/** Scope boundaries: </main> cannot close them, so what follows the fragment would land inside. */
const SCOPED = new Set(['template', 'select', 'table', 'object', 'marquee', 'applet']);
/** Start tags whose attributes the parser merges into the document's own element. */
const DOCUMENT_TAGS = new Set(['html', 'body']);
const FOREIGN_ROOTS = new Set(['svg', 'math']);
/**
 * After this many neutralised constructs every remaining '<' is escaped:
 * a scan for a missing terminator reads to the end, so this bounds the work
 * on hostile input (and such input has no markup worth keeping).
 */
const MAX_REPAIRS = 64;

function isAsciiAlpha(c: string): boolean {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

interface TagToken {
  /** Lower-cased tag name. */
  readonly name: string;
  /** Index just past the closing '>'. */
  readonly end: number;
  readonly selfClosing: boolean;
  readonly hasAttributes: boolean;
}

/**
 * The tag whose name starts at `k` (just after "<" or "</"), tokenized as a
 * browser does: the name runs to whitespace, '/' or '>'; quoted attribute
 * values may hold '>'. null when the fragment ends inside the tag.
 */
function readTag(s: string, k: number): TagToken | null {
  const n = s.length;
  let i = k;
  while (i < n && !WHITESPACE.has(s.charAt(i)) && s.charAt(i) !== '/' && s.charAt(i) !== '>') i++;
  const name = s.slice(k, i).toLowerCase();
  let selfClosing = false;
  let hasAttributes = false;
  for (;;) {
    // Before an attribute name: whitespace and stray '/' are skipped; "/>" closes the tag.
    while (i < n && (WHITESPACE.has(s.charAt(i)) || s.charAt(i) === '/')) {
      selfClosing = s.charAt(i) === '/';
      i++;
    }
    if (i >= n) return null;
    if (s.charAt(i) === '>') return { name, end: i + 1, selfClosing, hasAttributes };
    selfClosing = false;
    hasAttributes = true;
    // The attribute name (its first character may be '=', '"' or '<': all part of the name).
    i++;
    while (i < n && !WHITESPACE.has(s.charAt(i)) && s.charAt(i) !== '/' && s.charAt(i) !== '>' && s.charAt(i) !== '=') i++;
    while (i < n && WHITESPACE.has(s.charAt(i))) i++;
    if (i >= n) return null;
    if (s.charAt(i) !== '=') continue;
    i++;
    while (i < n && WHITESPACE.has(s.charAt(i))) i++;
    if (i >= n) return null;
    const q = s.charAt(i);
    if (q === '"' || q === "'") {
      const close = s.indexOf(q, i + 1);
      if (close < 0) return null;
      i = close + 1;
    } else {
      // Unquoted: runs to whitespace or '>' (quotes and '/' are part of the value).
      while (i < n && !WHITESPACE.has(s.charAt(i)) && s.charAt(i) !== '>') i++;
    }
  }
}

/** Index just past a comment whose body starts at `p` (right after its opener), or -1 when it never ends. */
function commentEnd(s: string, p: number): number {
  // "<!-->" and "<!--->" are complete (abruptly closed) empty comments.
  if (s.charAt(p) === '>') return p + 1;
  if (s.charAt(p) === '-' && s.charAt(p + 1) === '>') return p + 2;
  const re = /--!?>/g;
  re.lastIndex = p;
  const m = re.exec(s);
  return m ? m.index + m[0].length : -1;
}

/** Index of the "</name" that ends raw-text element `name` whose content starts at `p`, or -1. */
function rawTextEnd(s: string, p: number, name: string): number {
  // `name` comes from the fixed RAW_TEXT set, so it is a plain word.
  const re = new RegExp(`</${name}(?=[\\t\\n\\f\\r />])`, 'gi');
  re.lastIndex = p;
  const m = re.exec(s);
  return m ? m.index : -1;
}

/**
 * Index of the "</script" that really ends a script whose content starts at
 * `p`, or -1. Follows the tokenizer's script-data states: after an opening
 * comment marker the run is "escaped" until its closing marker, and inside
 * an escaped run a "<script" starts a double-escaped run in which "</script"
 * only returns to the escaped run.
 */
function scriptEnd(s: string, p: number): number {
  const re = /<!--(-*>)?|-->|<(\/?)script(?=[\t\n\f\r />])/gi;
  re.lastIndex = p;
  let state: 'data' | 'escaped' | 'double' = 'data';
  for (let m = re.exec(s); m !== null; m = re.exec(s)) {
    const t = m[0];
    if (t.charAt(1) === '!') {
      // An opener immediately closed ("-->" right after it) leaves the data state as it was.
      if (state === 'data' && m[1] === undefined) state = 'escaped';
      continue;
    }
    if (t.charAt(0) === '-') {
      state = 'data';
      continue;
    }
    if (m[2] === '/') {
      if (state === 'double') state = 'escaped';
      else return m.index;
      continue;
    }
    if (state === 'escaped') state = 'double';
  }
  return -1;
}

/** Open formatting and scoped elements, with a per-name count so a stray end tag costs O(1). */
class OpenElements {
  private readonly names: string[] = [];
  private readonly counts = new Map<string, number>();

  has(name: string): boolean {
    return (this.counts.get(name) ?? 0) > 0;
  }

  push(name: string): void {
    this.names.push(name);
    this.counts.set(name, (this.counts.get(name) ?? 0) + 1);
  }

  /** Remove the most recently opened `name`, if any. */
  close(name: string): void {
    if (!this.has(name)) return;
    const k = this.names.lastIndexOf(name);
    this.names.splice(k, 1);
    this.counts.set(name, (this.counts.get(name) ?? 1) - 1);
  }

  /** End tags for everything still open, innermost first. */
  closers(): string {
    return this.names.slice().reverse().map((name) => `</${name}>`).join('');
  }
}

/** Record a start tag that may stay open past the fragment's end. */
function trackStart(open: OpenElements, name: string): void {
  // A nested <select> acts as </select>; a second <a> or <nobr> closes the first.
  if (name === 'select' && open.has('select')) {
    open.close('select');
    return;
  }
  if ((name === 'a' || name === 'nobr') && open.has(name)) open.close(name);
  if (FORMATTING.has(name) || SCOPED.has(name)) open.push(name);
}

/**
 * The fragment with every construct that would reach past its end
 * neutralised (see the module comment). Returns the input unchanged when it
 * is already sealed; '' for null or undefined.
 */
export function sealHtmlFragment(html: string | null | undefined): string {
  const s = String(html ?? '');
  if (!s.includes('<')) return s;
  const n = s.length;
  const escapes: number[] = [];
  const open = new OpenElements();
  const foreign: string[] = [];
  let repairs = 0;
  const neutralise = (at: number): void => {
    escapes.push(at);
    repairs++;
  };
  /** A bogus comment ("<!x", "<?x", "</ x") runs to the next '>'. */
  const bogusComment = (lt: number, from: number): number => {
    const gt = s.indexOf('>', from);
    if (gt >= 0) return gt + 1;
    neutralise(lt);
    return from;
  };

  let i = 0;
  while (i < n) {
    const lt = s.indexOf('<', i);
    if (lt < 0) break;
    if (repairs >= MAX_REPAIRS) {
      escapes.push(lt);
      i = lt + 1;
      continue;
    }
    const c1 = s.charAt(lt + 1);

    if (c1 === '!') {
      if (s.startsWith('--', lt + 2)) {
        const end = commentEnd(s, lt + 4);
        if (end < 0) {
          neutralise(lt);
          i = lt + 4;
        } else {
          i = end;
        }
      } else if (s.startsWith('[CDATA[', lt + 2)) {
        const close = s.indexOf(']]>', lt + 9);
        if (close < 0) {
          neutralise(lt);
          i = lt + 9;
        } else {
          i = close + 3;
        }
      } else {
        i = bogusComment(lt, lt + 2);
      }
      continue;
    }
    if (c1 === '?') {
      i = bogusComment(lt, lt + 2);
      continue;
    }

    if (c1 === '/') {
      const c2 = s.charAt(lt + 2);
      if (c2 && isAsciiAlpha(c2)) {
        const tag = readTag(s, lt + 2);
        if (!tag) {
          neutralise(lt);
          i = lt + 2;
          continue;
        }
        if (FOREIGN_ROOTS.has(tag.name) && foreign.includes(tag.name)) foreign.length = foreign.lastIndexOf(tag.name);
        else open.close(tag.name);
        i = tag.end;
      } else if (c2 === '>') {
        i = lt + 3; // "</>" is dropped by the parser
      } else if (c2 === '') {
        i = n; // "</" at the very end is text
      } else {
        i = bogusComment(lt, lt + 2);
      }
      continue;
    }

    if (!c1 || !isAsciiAlpha(c1)) {
      i = lt + 1; // '<' before anything but a letter, '!', '?' or '/' is text
      continue;
    }
    const tag = readTag(s, lt + 1);
    if (!tag) {
      neutralise(lt);
      i = lt + 1;
      continue;
    }
    i = tag.end;
    const name = tag.name;
    if (DOCUMENT_TAGS.has(name)) {
      if (tag.hasAttributes) neutralise(lt);
      continue;
    }
    if (FOREIGN_ROOTS.has(name)) {
      if (!tag.selfClosing) foreign.push(name);
      continue;
    }
    if (!foreign.length) {
      if (name === 'plaintext') {
        neutralise(lt);
        continue;
      }
      if (RAW_TEXT.has(name)) {
        const close = name === 'script' ? scriptEnd(s, tag.end) : rawTextEnd(s, tag.end, name);
        const closing = close < 0 ? null : readTag(s, close + 2);
        if (closing) {
          i = closing.end;
        } else {
          // Shown as text instead; its content is read as markup again.
          neutralise(lt);
        }
        continue;
      }
    }
    trackStart(open, name);
  }

  let out = s;
  if (escapes.length) {
    const at = Array.from(new Set(escapes)).sort((a, b) => a - b);
    const parts: string[] = [];
    let last = 0;
    for (const k of at) {
      parts.push(s.slice(last, k), '&lt;');
      last = k + 1;
    }
    parts.push(s.slice(last));
    out = parts.join('');
  }
  return out + open.closers();
}
