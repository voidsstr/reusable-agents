/**
 * Rough HTML checks for the render tests: htmlProblems (tags balanced and
 * properly nested, void elements and self-closing tags allowed, script and
 * style contents skipped, no block element inside a <p>; [] when the markup
 * is sound) and browserVisibleText (what a page still shows once comments and
 * raw-text elements have taken what they swallow).
 *
 * Not a test file (jest runs *.test.ts only).
 */
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);
const BLOCK = new Set(['address', 'article', 'aside', 'blockquote', 'details', 'div', 'dl', 'fieldset', 'figure', 'footer',
  'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'main', 'nav', 'ol', 'p', 'pre', 'section', 'table', 'ul']);

export function htmlProblems(html: string): string[] {
  const problems: string[] = [];
  const src = html
    .replace(/<!doctype[^>]*>/i, '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<(script|style)\b([^>]*)>[\s\S]*?<\/\1\s*>/gi, '<$1$2></$1>');
  const stack: string[] = [];
  for (const m of src.matchAll(/<(\/?)([A-Za-z][A-Za-z0-9-]*)\b((?:[^>"']|"[^"]*"|'[^']*')*)>/g)) {
    const closing = m[1] === '/';
    const name = (m[2] as string).toLowerCase();
    const selfClosing = /\/\s*$/.test(m[3] ?? '');
    if (closing) {
      if (VOID.has(name)) { problems.push(`</${name}> closes a void element`); continue; }
      const top = stack.pop();
      if (top !== name) problems.push(`</${name}> closes <${top ?? 'nothing'}>`);
      continue;
    }
    if (BLOCK.has(name) && stack[stack.length - 1] === 'p') problems.push(`<${name}> inside <p>`);
    if (!VOID.has(name) && !selfClosing) stack.push(name);
  }
  if (stack.length) problems.push(`unclosed: ${stack.join(' > ')}`);
  return problems;
}

const HIDING_OPEN_RE = /<!--|<(script|style|template|textarea|title|noscript|xmp|iframe|noembed|noframes)\b[^>]*>|<plaintext\b/gi;

/**
 * The text a browser would still show from a document's <body>, modelling
 * only what can hide everything after it: a comment runs to its "-->", a
 * raw-text element or template to its end tag, plaintext forever, and a
 * missing terminator hides the rest of the page. Tags are then dropped and
 * the common entities decoded. A rough model for the tests, deliberately
 * simpler than (and independent of) render/seal.ts.
 */
export function browserVisibleText(html: string): string {
  const body = html.slice(Math.max(0, html.indexOf('<body')));
  let out = '';
  let i = 0;
  HIDING_OPEN_RE.lastIndex = 0;
  for (let m = HIDING_OPEN_RE.exec(body); m !== null; m = HIDING_OPEN_RE.exec(body)) {
    if (m.index < i) continue;
    out += body.slice(i, m.index);
    const name = m[1]?.toLowerCase();
    const close = m[0].startsWith('<!--') ? body.indexOf('-->', m.index + 4)
      : name ? body.toLowerCase().indexOf(`</${name}`, m.index + m[0].length) : -1;
    if (close < 0) return decode(out);
    const end = body.indexOf('>', close);
    if (end < 0) return decode(out);
    i = end + 1;
    HIDING_OPEN_RE.lastIndex = i;
  }
  return decode(out + body.slice(i));
}

function decode(html: string): string {
  return html.replace(/<[^>]+>/g, ' ')
    .replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&copy;/g, '©').replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ');
}
