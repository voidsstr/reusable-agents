/**
 * A minimal XML well-formedness check for the sitemap tests (no XML parser is
 * a kit dependency): one root element, balanced and properly nested tags,
 * quoted attributes, no stray '<' or '&' in text, only predefined or numeric
 * entities. Returns the problems found ([] when well-formed).
 *
 * Not a test file (jest runs *.test.ts only).
 */
export function xmlProblems(xml: string): string[] {
  const problems: string[] = [];
  let s = xml.replace(/^\ufeff/, '');
  const decl = /^<\?xml\s[^?]*\?>\s*/.exec(s);
  if (decl) s = s.slice(decl[0].length);
  const stack: string[] = [];
  let roots = 0;
  const TOKEN = /<!--([\s\S]*?)-->|<\/([A-Za-z_][\w:.-]*)\s*>|<([A-Za-z_][\w:.-]*)((?:\s+[A-Za-z_][\w:.-]*\s*=\s*(?:"[^"<]*"|'[^'<]*'))*)\s*(\/?)>|([^<]+)|(<)/g;
  let m: RegExpExecArray | null;
  while ((m = TOKEN.exec(s)) !== null) {
    const [, comment, close, open, , selfClose, textRun, stray] = m;
    if (comment !== undefined) {
      if (comment.includes('--')) problems.push('comment contains "--"');
    } else if (close) {
      const top = stack.pop();
      if (top !== close) problems.push(`</${close}> closes <${top ?? 'nothing'}>`);
    } else if (open) {
      if (!stack.length) roots++;
      if (!selfClose) stack.push(open);
    } else if (textRun !== undefined) {
      if (!stack.length && textRun.trim()) problems.push(`text outside the root: ${textRun.trim().slice(0, 40)}`);
      for (const e of textRun.matchAll(/&([^;\s]*);?/g)) {
        if (!/^(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+)$/.test(e[1] ?? '') || !e[0].endsWith(';')) problems.push(`bad entity ${e[0]}`);
      }
    } else if (stray) {
      problems.push(`stray '<' at ${m.index}`);
    }
  }
  if (stack.length) problems.push(`unclosed <${stack.join('>, <')}>`);
  if (roots !== 1) problems.push(`${roots} root elements`);
  return problems;
}
