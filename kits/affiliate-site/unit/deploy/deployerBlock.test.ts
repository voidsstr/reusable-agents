/**
 * The deployer block deploy/README.md tells a site to copy into its site.yaml
 * ("Routine deploys"), against the deployer it is written for
 * (reusable-agents agents/deployer/deployer.py, read here, never run).
 *
 * When deploy.cmd exits non-zero and deploy.vars has both app and rg, the
 * deployer reads the image the app's active revision runs and, if it ends
 * with the new tag, records the deploy as a success. The kit's deploy.sh can
 * exit 4 with the new image still serving (a smoke failure whose automatic
 * rollback itself failed), so the block leaves rg out: every non-zero exit is
 * then a failed deploy. The tripwire at the end fails when the deployer's
 * condition changes, so the README is revisited with it.
 */
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

const KIT = path.resolve(__dirname, '..', '..');
const README = path.join(KIT, 'deploy', 'README.md');
/** unit/ is never vendored into a site, so this always resolves inside reusable-agents. */
const DEPLOYER = path.resolve(KIT, '..', '..', 'agents', 'deployer', 'deployer.py');

/** The ```yaml fence of deploy/README.md whose first line is `deployer:`. */
function deployerBlock(): string {
  const blocks = [...fs.readFileSync(README, 'utf8').matchAll(/^```yaml\n([\s\S]*?)^```$/gm)].map((m) => m[1] as string);
  const mine = blocks.filter((b) => b.startsWith('deployer:\n'));
  expect(mine).toHaveLength(1);
  return mine[0] as string;
}

/** `key: value` of one stage (two-space indent under deployer:, four under the stage). */
function stageField(block: string, stage: string, key: string): string {
  const m = block.match(new RegExp(`^  ${stage}:\\n((?:    .*\\n)+)`, 'm'));
  expect([stage, !!m]).toEqual([stage, true]);
  const f = (m as RegExpMatchArray)[1]!.match(new RegExp(`^    ${key}: (.*)$`, 'm'));
  expect([`${stage}.${key}`, !!f]).toEqual([`${stage}.${key}`, true]);
  return (f as RegExpMatchArray)[1] as string;
}

const pythonYaml = spawnSync('python3', ['-c', 'import yaml'], { encoding: 'utf8' }).status === 0;

describe("deploy/README.md's deployer block", () => {
  const block = deployerBlock();

  it('names the app (the deploy lock) and the image, and leaves rg out so a non-zero exit is never read back as a success', () => {
    const vars = stageField(block, 'deploy', 'vars');
    expect(vars).toMatch(/^\{.*\}$/);
    const keys = vars.slice(1, -1).split(',').map((kv) => kv.split(':')[0]!.trim());
    expect(keys.sort()).toEqual(['app', 'image']);
    expect(block).not.toMatch(/\{rg\}/);
    expect(block).not.toMatch(/^\s*rg:/m);
  });

  it('deploys migrate, release and smoke with the automatic rollback; the rollback releases only, with no smoke stage', () => {
    const deploy = stageField(block, 'deploy', 'cmd');
    expect(deploy).toContain('--only migrate,release,smoke');
    expect(deploy).toContain('--rollback-on-smoke-failure');
    const rollback = stageField(block, 'rollback', 'cmd');
    expect(rollback).toMatch(/--only release(\s|$)/);
    expect(rollback).not.toMatch(/smoke/);
    expect(rollback).toContain('--image-tag {prior_image}');
    // The capture command reads the serving image with the resource group written out.
    expect(stageField(block, 'rollback', 'capture_cmd'))
      .toBe('az containerapp show --name {app} --resource-group <AZ_RESOURCE_GROUP> --query "properties.template.containers[0].image" -o tsv');
  });

  (pythonYaml ? it : it.skip)('parses as YAML into the same shape', () => {
    const r = spawnSync('python3', ['-c', `
import json, sys, yaml
d = yaml.safe_load(sys.stdin.read())["deployer"]
print(json.dumps({"stages": sorted(d), "vars": sorted(d["deploy"]["vars"]), "rollback": sorted(d["rollback"])}))
`], { input: block, encoding: 'utf8' });
    expect([r.status, r.stderr]).toEqual([0, '']);
    expect(JSON.parse(r.stdout)).toEqual({
      stages: ['build', 'deploy', 'push', 'rollback', 'smoke_check', 'test'],
      vars: ['app', 'image'],
      rollback: ['capture_cmd', 'cmd'],
    });
  });
});

describe('the deployer this block is written for (tripwire)', () => {
  it('reads the serving image after a failed deploy only when deploy.vars has both app and rg', () => {
    expect(fs.existsSync(DEPLOYER)).toBe(true);
    const src = fs.readFileSync(DEPLOYER, 'utf8');
    // If any of these change, revisit "vars has no rg" in deploy/README.md and this test.
    expect(src).toContain('app = deploy_vars.get("app", "")');
    expect(src).toContain('rg = deploy_vars.get("rg", "")');
    expect(src).toContain('if app and rg:');
    expect(src).toContain('if rc != 0 and not verified_via_revision:');
    // rg is read in that one place only.
    expect(src.match(/deploy_vars\.get\("rg"/g)).toHaveLength(1);
    expect(src.match(/\["rg"\]/g)).toBeNull();
  });
});
