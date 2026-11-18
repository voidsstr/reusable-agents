import { collapse, looksLikePrice } from './compliance';

/**
 * llms.txt (llmstxt.org) checked as an assistant reads it: the site's name as
 * the H1, a '>' summary, the earnings disclosure, absolute links whose
 * on-site targets exist, and no price (the file is cached for an hour and has
 * nowhere to put the 2(i) time and disclaimer).
 */

export interface LlmsExpectations {
  brandName: string;
  /** site.json disclosure.earnings. */
  earnings: string;
  /** https origin of the site, no trailing slash. */
  canonicalOrigin: string;
}

export interface LlmsCheck {
  problems: string[];
  /** On-site link targets (absolute, on the canonical origin) the caller should resolve. */
  siteLinks: string[];
}

export const LLMS_MAX_BYTES = 1024 * 1024;

export function llmsProblems(text: string, exp: LlmsExpectations): LlmsCheck {
  const problems: string[] = [];
  const siteLinks: string[] = [];
  const body = String(text ?? '');
  if (!body.trim()) return { problems: ['llms.txt is empty'], siteLinks };
  if (Buffer.byteLength(body, 'utf8') > LLMS_MAX_BYTES) problems.push(`llms.txt is ${Buffer.byteLength(body, 'utf8')} bytes (over ${LLMS_MAX_BYTES})`);
  const lines = body.split(/\r?\n/);
  const first = lines.find((l) => l.trim()) ?? '';
  if (first.trim() !== `# ${exp.brandName}`) problems.push(`llms.txt starts "${first.trim().slice(0, 60)}", expected "# ${exp.brandName}"`);
  const firstSection = lines.findIndex((l) => /^##\s/.test(l));
  const head = firstSection < 0 ? lines : lines.slice(0, firstSection);
  if (!head.some((l) => /^>\s*\S/.test(l))) problems.push('llms.txt has no "> summary" line before its first section');
  if (!collapse(body).includes(collapse(exp.earnings))) problems.push('llms.txt does not carry the earnings disclosure');
  lines.forEach((l, i) => {
    if (looksLikePrice(l)) problems.push(`llms.txt line ${i + 1} quotes a price (no 2(i) time or disclaimer there): "${l.trim().slice(0, 100)}"`);
  });
  for (const m of body.matchAll(/\[[^\]\n]*\]\(([^)\s]+)\)/g)) {
    const url = m[1] as string;
    let u: URL;
    try {
      u = new URL(url);
    } catch {
      problems.push(`llms.txt link "${url.slice(0, 80)}" is not an absolute URL`);
      continue;
    }
    if (u.protocol !== 'https:' && u.protocol !== 'http:') problems.push(`llms.txt link "${url.slice(0, 80)}" is not http(s)`);
    else if (u.origin === exp.canonicalOrigin) siteLinks.push(u.toString());
  }
  for (const m of body.matchAll(/^- Sitemap:\s*(\S+)/gm)) {
    const url = m[1] as string;
    if (!url.startsWith(`${exp.canonicalOrigin}/`)) problems.push(`llms.txt sitemap ${url} is not on ${exp.canonicalOrigin}`);
    else siteLinks.push(url);
  }
  return { problems, siteLinks: [...new Set(siteLinks)] };
}
