import * as fs from 'fs';
import * as path from 'path';
import { RENDER, SITE_TEMPLATE, cleanupTmp, describeRun, filesUnder, run, tmpDir } from './helpers';

/**
 * _template/affiliate-site/scaffold/render_template.py: the placeholder
 * renderer create-affiliate-site.sh uses. Every rule its docstring states is
 * pinned here on a small template of its own.
 */

function render(template: string, out: string, vars: Record<string, string>) {
  return run('python3', ['-I', RENDER, '--template', template, '--out', out, ...Object.entries(vars).flatMap(([k, v]) => ['--var', `${k}=${v}`])]);
}

function write(root: string, rel: string, text: string, mode = 0o644): void {
  const p = path.join(root, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, text);
  fs.chmodSync(p, mode);
}

describe('render_template.py', () => {
  let tmp = '';
  let tpl = '';
  beforeEach(() => {
    tmp = tmpDir('render');
    tpl = path.join(tmp, 'tpl');
  });
  afterEach(() => cleanupTmp(tmp));

  it('renders .tmpl files, copies the rest byte for byte, and renames dot- and __NICHE__ segments', () => {
    write(tpl, 'a.txt.tmpl', 'slug={{SLUG}}\n');
    write(tpl, 'raw.txt', 'kept {{SLUG}} as is\n');
    write(tpl, 'dot-gitignore', 'node_modules/\n');
    write(tpl, 'src/__NICHE__/policy.ts.tmpl', "export const N = '{{NICHE}}';\n");
    write(tpl, 'tools/run.sh', '#!/usr/bin/env bash\necho hi\n', 0o600);
    const out = path.join(tmp, 'out');
    const r = render(tpl, out, { SLUG: 'shop', NICHE: 'widgets' });
    expect(r.status).toBe(0);
    expect(JSON.parse(r.stdout).files).toEqual(['.gitignore', 'a.txt', 'raw.txt', 'src/widgets/policy.ts', 'tools/run.sh']);
    expect(fs.readFileSync(path.join(out, 'a.txt'), 'utf8')).toBe('slug=shop\n');
    expect(fs.readFileSync(path.join(out, 'raw.txt'), 'utf8')).toBe('kept {{SLUG}} as is\n');
    expect(fs.readFileSync(path.join(out, 'src/widgets/policy.ts'), 'utf8')).toBe("export const N = 'widgets';\n");
    // Modes come from the renderer, never from the template checkout.
    expect(fs.statSync(path.join(out, 'tools/run.sh')).mode & 0o777).toBe(0o755);
    expect(fs.statSync(path.join(out, 'a.txt')).mode & 0o777).toBe(0o644);
    expect(fs.statSync(path.join(out, 'src/widgets')).mode & 0o777).toBe(0o755);
  });

  it('quotes free text through its encoder: JSON, SQL, JSON lists and shell words', () => {
    write(tpl, 'x.json.tmpl', '{"brand": {{json:BRAND}}, "sets": {{jsonlist:SETS}}, "none": {{jsonlist:EMPTY}}}\n');
    write(tpl, 'x.sql.tmpl', 'SELECT {{sql:BRAND}};\n');
    write(tpl, 'x.yaml.tmpl', 'name: {{json:BRAND}}\n');
    write(tpl, 'x.env.tmpl', 'LOCKS={{shq:LOCKS}}\nBRAND={{shq:BRAND}}\n');
    const out = path.join(tmp, 'out');
    const brand = `Joe's "Best" Picks & Co`;
    const r = render(tpl, out, { BRAND: brand, SETS: 'electrical, medical', EMPTY: '', LOCKS: 'one-gate two_gate *' });
    expect(r.status).toBe(0);
    expect(JSON.parse(fs.readFileSync(path.join(out, 'x.json'), 'utf8'))).toEqual({ brand, sets: ['electrical', 'medical'], none: [] });
    expect(fs.readFileSync(path.join(out, 'x.sql'), 'utf8')).toBe(`SELECT 'Joe''s "Best" Picks & Co';\n`);
    expect(fs.readFileSync(path.join(out, 'x.yaml'), 'utf8')).toBe(`name: ${JSON.stringify(brand)}\n`);
    expect(fs.readFileSync(path.join(out, 'x.env'), 'utf8')).toBe(`LOCKS='one-gate two_gate *'\nBRAND='Joe'\\''s "Best" Picks & Co'\n`);
    // Sourced by bash, each is one word, read back exactly, nothing expanded.
    const sourced = run('bash', ['-c', 'set -u; source "$1"; printf "%s\\n%s\\n" "$LOCKS" "$BRAND"', 'x', path.join(out, 'x.env')]);
    expect(sourced.stdout).toBe(`one-gate two_gate *\n${brand}\n`);
  });

  it('refuses a bare placeholder whose value would need quoting, except in Markdown', () => {
    write(tpl, 'x.ts.tmpl', "const brand = '{{BRAND}}';\n");
    const r = render(tpl, path.join(tmp, 'out'), { BRAND: "Joe's Picks" });
    expect(r.status).toBe(1);
    expect(r.stderr).toMatch(/would insert .* unquoted; use \{\{json:BRAND\}\}/);
    expect(fs.existsSync(path.join(tmp, 'out'))).toBe(false);

    const tpl2 = path.join(tmp, 'tpl2');
    write(tpl2, 'README.md.tmpl', '# {{BRAND}}\n');
    const r2 = render(tpl2, path.join(tmp, 'out2'), { BRAND: "Joe's Picks" });
    expect(r2.status).toBe(0);
    expect(fs.readFileSync(path.join(tmp, 'out2', 'README.md'), 'utf8')).toBe("# Joe's Picks\n");
  });

  it('refuses unknown placeholders and encoders, and writes nothing on any error', () => {
    write(tpl, 'ok.txt.tmpl', '{{SLUG}}\n');
    write(tpl, 'bad.txt.tmpl', '{{NOPE}}\n');
    let r = render(tpl, path.join(tmp, 'out'), { SLUG: 'x' });
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('unknown placeholder {{NOPE}}');
    expect(fs.existsSync(path.join(tmp, 'out'))).toBe(false);

    const tpl2 = path.join(tmp, 'tpl2');
    write(tpl2, 'bad.txt.tmpl', '{{yaml:SLUG}}\n');
    r = render(tpl2, path.join(tmp, 'out2'), { SLUG: 'x' });
    expect(r.status).toBe(1);
    expect(r.stderr).toContain("unknown encoder 'yaml'");
  });

  it('refuses values that could smuggle a placeholder or break a line, and bad list items', () => {
    write(tpl, 'a.txt.tmpl', '{{X}}\n');
    expect(render(tpl, path.join(tmp, 'o1'), { X: 'a{{B}}' }).status).toBe(1);
    expect(render(tpl, path.join(tmp, 'o2'), { X: 'a\nb' }).status).toBe(1);
    const tpl2 = path.join(tmp, 'tpl2');
    write(tpl2, 'a.json.tmpl', '{{jsonlist:X}}\n');
    const r = render(tpl2, path.join(tmp, 'o3'), { X: 'ok, not ok' });
    expect(r.status).toBe(1);
    expect(r.stderr).toContain("list item 'not ok'");
  });

  it('refuses an output directory that is not empty, and symbolic links in the template', () => {
    write(tpl, 'a.txt', 'x\n');
    const out = path.join(tmp, 'out');
    fs.mkdirSync(out);
    fs.writeFileSync(path.join(out, 'keep.txt'), 'mine\n');
    const r = render(tpl, out, {});
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('is not an empty directory');
    expect(fs.readdirSync(out)).toEqual(['keep.txt']);

    const tpl2 = path.join(tmp, 'tpl2');
    write(tpl2, 'a.txt', 'x\n');
    fs.symlinkSync('/etc/hostname', path.join(tpl2, 'link.txt'));
    const r2 = render(tpl2, path.join(tmp, 'out2'), {});
    expect(r2.status).toBe(1);
    expect(r2.stderr).toContain('symbolic link');
  });

  it('usage errors exit 2', () => {
    expect(run('python3', ['-I', RENDER]).status).toBe(2);
    write(tpl, 'a.txt', 'x\n');
    const r = run('python3', ['-I', RENDER, '--template', tpl, '--out', path.join(tmp, 'o'), '--var', 'lower=x']);
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('upper-case NAME');
  });
});

describe('the site template itself', () => {
  const files = filesUnder(SITE_TEMPLATE, []);

  it('holds the files the scaffold contract names', () => {
    for (const f of [
      'site.json.tmpl', 'package.json.tmpl', 'tsconfig.json', 'tsconfig.build.json', 'src/index.ts', 'src/routes/table.ts',
      'src/__NICHE__/policy.ts.tmpl', 'db/migrations/0000_site_defaults.sql.tmpl', 'tests/suite.env.tmpl',
      'tests/run-local-suite.sh', 'tests/unit/kit-integrity.test.ts', 'agents/register-with-framework.sh',
      'agents/implementer-scope.yaml.tmpl', 'CLAUDE.md.tmpl', 'keep-the-lights-on.yaml.tmpl', 'azure/deploy.sh', 'aws/deploy.sh',
      'dot-gitignore',
    ]) expect(files).toContain(f);
  });

  it('names no site: niche values are placeholders or come from site.json', () => {
    const offenders = files.filter((f) => /specpicks|aisleprompt|runtimepicks/i.test(fs.readFileSync(path.join(SITE_TEMPLATE, f), 'utf8')));
    expect(offenders).toEqual([]);
  });

  it('has no literal dotfiles that would act on this checkout (dot- names are renamed when rendered)', () => {
    expect(files.filter((f) => f.split('/').some((seg) => seg.startsWith('.')))).toEqual([]);
  });

  it('every placeholder it uses is one create-affiliate-site.sh passes', () => {
    const script = fs.readFileSync(path.join(SITE_TEMPLATE, '..', '..', '..', 'install', 'create-affiliate-site.sh'), 'utf8');
    const passed = new Set([...script.matchAll(/--var "([A-Z_]+)=/g)].map((m) => m[1] as string));
    const used = new Set<string>();
    for (const f of files.filter((x) => x.endsWith('.tmpl'))) {
      for (const m of fs.readFileSync(path.join(SITE_TEMPLATE, f), 'utf8').matchAll(/\{\{\s*(?:[a-z]+:)?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g)) used.add(m[1] as string);
    }
    expect([...used].filter((v) => !passed.has(v))).toEqual([]);
    expect(used.size).toBeGreaterThan(10);
  });

  it('renders cleanly with the variables the script passes (no leftover placeholder)', () => {
    const tmp = tmpDir('render-site');
    try {
      const vars: Record<string, string> = {
        SLUG: 'shop', DOMAIN: 'shop.example', BRAND: "Joe's Shop", BYLINE: "Joe's Shop Editorial Team", DISPLAY_NAME: "Joe's Shop (shop.example)",
        DESCRIPTION: 'd', EARNINGS: "As an Amazon Associate, Joe's Shop earns from qualifying purchases.", PARTNER_TAG: 'shop-20',
        NICHE: 'widgets', POLICY_FILE: 'src/widgets/policy.ts', GATE_PORT: '10031', FIXTURE_DB_PORT: '5433', FIXTURE_DB_NAME: 'shop_fixture',
        FIXTURE_DB_USER: 'shop_fixture', FIXTURE_DB_PASSWORD: 'shop_fixture', CONTENT_RULE_SETS: '', CONTENT_POLICY_WAIVED: "Joe's site has no articles",
        CONTENT_RULE_SETS_TEXT: "none: the content gate is waived (Joe's site has no articles)", HOST_LOCKS: 'other-gate *',
        INDEXNOW_KEY: '0f4c2a9e7b1d4e3a', KIT_VERSION: '0.1.0', SITE_DIR: '/srv/shop', ENV_IDENT: 'SHOP', CREATED_ON: '2026-11-17',
      };
      const r = render(SITE_TEMPLATE, path.join(tmp, 'out'), vars);
      expect(r.status).toBe(0);
      if (r.status !== 0) throw new Error(describeRun(r));
      const out = filesUnder(path.join(tmp, 'out'), []);
      expect(out).toContain('src/widgets/policy.ts');
      expect(out).toContain('.gitignore');
      for (const f of out) expect(fs.readFileSync(path.join(tmp, 'out', f), 'utf8')).not.toMatch(/\{\{\s*(?:[a-z]+:)?[A-Za-z_]\w*\s*\}\}/);
    } finally {
      cleanupTmp(tmp);
    }
  });
});
