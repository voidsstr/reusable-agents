/**
 * The release and domain control flow in REAL mode against recording stubs that
 * answer like az / aws do: waiting for the new revision / rollout, the ingress
 * port guard, the ownership tag, failure handling with the rollback hint, the
 * smoke-failure rollback, the hostname binding sequence, and how existence
 * probes read errors. The smoke checks here run for real against names under
 * .invalid (RFC 6761: they never resolve), so they fail fast and offline.
 */
import { spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { editedEnv, makeSandbox, removeSandbox, runScript, setResponses, type RunResult, type Sandbox } from './harness';

let sb: Sandbox;
beforeEach(() => {
  sb = makeSandbox();
});
afterEach(() => removeSandbox(sb));

const PRIOR = 'kittestacrdoesnotexist.azurecr.io/kitexample:20261101-000000';
const AZ_OWNER = { match: 'tags."affiliate-kit-app"', stdout: 'kitexample\n' };

function azCalls(r: RunResult): string[] {
  return r.calls.filter((c) => c.tool === 'az').map((c) => c.argv.join(' '));
}

describe('azure/deploy.sh --only release (real mode)', () => {
  const base = [
    AZ_OWNER,
    { match: 'az acr repository show', stdout: 'sha256:0123\n' },
    { match: '--query name -o tsv', stdout: 'kitexample\n' },
    { match: 'ingress show', stdout: '8080\n' },
    { match: 'properties.template.containers[0].image', stdout: `${PRIOR}\n` },
    { match: '--query properties.latestRevisionName', stdout: 'kitexample--r2\n' },
    { match: 'ingress.fqdn', stdout: 'kitexample.test-env.example.invalid\n' },
  ];

  it('updates the revision and returns once it is the latest ready revision', () => {
    setResponses(sb, [...base, { match: 'latestReadyRevisionName', stdout: 'kitexample--r2\n' }]);
    const r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--only', 'release', '--image-tag', 't9']);
    expect(r.status).toBe(0);
    const calls = azCalls(r);
    expect(calls.findIndex((c) => c.startsWith('containerapp update'))).toBeGreaterThan(calls.findIndex((c) => c.startsWith('containerapp ingress show')));
    expect(calls.find((c) => c.startsWith('containerapp update'))).toContain('--image kittestacrdoesnotexist.azurecr.io/kitexample:t9');
    expect(r.stdout).toContain('revision kitexample--r2 is serving kittestacrdoesnotexist.azurecr.io/kitexample:t9');
    expect(r.stdout.trim().split('\n').pop()).toBe(
      '{"app_name": "kitexample", "url": "https://kitexample.test-env.example.invalid", "image": "kittestacrdoesnotexist.azurecr.io/kitexample:t9"}');
  });

  it('stops with the previous image and the command to restore it when the revision fails', () => {
    setResponses(sb, [...base, { match: 'latestReadyRevisionName', stdout: 'kitexample--r1\n' },
      { match: 'revision show', stdout: 'Provisioned\tFailed\n' }]);
    const r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--only', 'release', '--image-tag', 't9']);
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('revision kitexample--r2: Provisioned Failed');
    expect(r.stderr).toContain(`the previous image was ${PRIOR}`);
    expect(r.stderr).toContain('--only release,smoke --image-tag 20261101-000000');
  });

  it('refuses to release when the ingress port differs from TARGET_PORT (the revision could never activate)', () => {
    setResponses(sb, [{ match: 'ingress show', stdout: '4000\n' }, ...base]);
    const r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--only', 'release', '--image-tag', 't9']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('ingress targetPort is 4000 but TARGET_PORT is 8080');
    expect(azCalls(r).some((c) => c.startsWith('containerapp update'))).toBe(false);
  });

  it('refuses to release an image that is not in the registry', () => {
    setResponses(sb, base.filter((b) => b.match !== 'az acr repository show'));
    const r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--only', 'release', '--image-tag', 'missing']);
    expect(r.status).toBe(3);
    expect(r.stderr).toContain('kittestacrdoesnotexist.azurecr.io/kitexample:missing is not in kittestacrdoesnotexist');
  });
});

describe('azure/provision.sh domain (real mode, DNS check skipped)', () => {
  it('adds and binds every hostname that is not bound yet, apex over HTTP and www over CNAME', () => {
    setResponses(sb, [
      AZ_OWNER,
      { match: 'ingress.fqdn', stdout: 'kitexample.test-env.example.invalid\n' },
      { match: 'customDomainVerificationId', stdout: 'ABCDEF0123\n' },
      { match: 'properties.staticIp', stdout: '203.0.113.10\n' },
      { match: "[?name=='kit-example.test'].bindingType", stdout: 'SniEnabled\n' },
    ]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'domain', '--skip-dns-check']);
    expect(r.status).toBe(0);
    expect(r.stdout).toMatch(/A +kit-example\.test +203\.0\.113\.10/);
    expect(r.stdout).toMatch(/TXT +asuid\.www\.kit-example\.test +ABCDEF0123/);
    expect(r.stdout).toContain('kit-example.test: already bound with a certificate');
    const calls = azCalls(r).filter((c) => c.startsWith('containerapp hostname add') || c.startsWith('containerapp hostname bind'));
    expect(calls).toEqual([
      'containerapp hostname add --hostname www.kit-example.test --resource-group kit-test-rg-does-not-exist --name kitexample -o none',
      'containerapp hostname bind --hostname www.kit-example.test --resource-group kit-test-rg-does-not-exist --name kitexample --environment kit-test-env-does-not-exist --validation-method CNAME -o none',
    ]);
  });
});

describe('azure/provision.sh domain: the DNS check (real mode, local DNS-over-HTTPS stub)', () => {
  /** A DoH JSON stub in its own process (runScript blocks this one): the apex
   * answers with a proxied-looking address, anything under www. fails (500). */
  const DOH = `import http.server, json, urllib.parse
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        name, rtype = q.get("name", [""])[0], q.get("type", [""])[0]
        if "www." in name:
            self.send_response(500); self.end_headers(); return
        ans = {"A": [{"type": 1, "data": "104.21.1.1"}], "TXT": [{"type": 16, "data": '"ABCDEF0123"'}]}.get(rtype, [])
        body = json.dumps({"Status": 0, "Answer": ans}).encode()
        self.send_response(200); self.send_header("content-type", "application/dns-json"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
s = http.server.HTTPServer(("127.0.0.1", 0), H)
print(s.server_port, flush=True)
s.serve_forever()
`;
  const answers = [
    AZ_OWNER,
    { match: 'ingress.fqdn', stdout: 'kitexample.test-env.example.invalid\n' },
    { match: 'customDomainVerificationId', stdout: 'ABCDEF0123\n' },
    { match: 'properties.staticIp', stdout: '203.0.113.10\n' },
  ];

  it('records known to be wrong stop the bind even when a later hostname could not be checked', async () => {
    const script = path.join(sb.root, 'doh.py');
    fs.writeFileSync(script, DOH);
    const doh = spawn('python3', ['-I', script], { stdio: ['ignore', 'pipe', 'inherit'] });
    try {
      const port = await new Promise<string>((resolve) => doh.stdout.once('data', (d) => resolve(String(d).trim())));
      setResponses(sb, answers);
      const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'domain'], { KIT_DEPLOY_DOH_URL: `http://127.0.0.1:${port}/dns-query` });
      expect(r.stdout).toContain('WRONG A      kit-example.test -> 104.21.1.1');
      expect(r.status).toBe(3);
      expect(r.stderr).toContain('the DNS records above are not in place yet');
      expect(azCalls(r).filter((c) => /^containerapp hostname (add|bind)/.test(c))).toEqual([]);
    } finally {
      doh.kill();
    }
  });

  it('when no hostname could be checked at all, it binds and lets the bind validate', () => {
    setResponses(sb, answers);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'domain'], { KIT_DEPLOY_DOH_URL: 'http://127.0.0.1:9/dns-query' });
    expect(r.status).toBe(0);
    expect(r.stderr).toContain('could not check DNS for every hostname');
    expect(azCalls(r).filter((c) => c.startsWith('containerapp hostname bind'))).toHaveLength(2);
  });
});

describe('aws/deploy.sh --only release (real mode)', () => {
  const NEW_TD = 'arn:aws:ecs:us-east-1:123456789012:task-definition/kitexample:8';
  const OLD_TD = 'arn:aws:ecs:us-east-1:123456789012:task-definition/kitexample:7';
  const base = [
    { match: '--include TAGS', stdout: 'kitexample\n' },
    { match: 'describe-task-definition', stdout: '123456789012.dkr.ecr.us-east-1.amazonaws.com/kitexample:20261101-000000\n' },
    { match: 'ecr describe-images', stdout: 'sha256:0123\n' },
    { match: "services[?status=='ACTIVE'].serviceName", stdout: 'kitexample\n' },
    { match: "--query services[0].taskDefinition", stdout: `${OLD_TD}\n` },
    { match: 'secretsmanager describe-secret', stdout: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:nsc/kitexample-AbCdEf\n' },
    { match: 'register-task-definition', stdout: `${NEW_TD}\n` },
  ];

  it('registers the task definition, updates the service and waits for COMPLETED', () => {
    setResponses(sb, [{ match: "deployments[?status=='PRIMARY']", stdout: `${NEW_TD}\tCOMPLETED\n` }, ...base]);
    const r = runScript(sb, 'aws/deploy.sh', ['--env', sb.envFile, '--only', 'release', '--image-tag', 't9']);
    expect(r.status).toBe(0);
    const aws = r.calls.filter((c) => c.tool === 'aws').map((c) => c.argv.join(' '));
    expect(aws.find((c) => c.startsWith('ecs update-service'))).toContain(`--task-definition ${NEW_TD}`);
    const reg = r.calls.find((c) => c.tool === 'aws' && c.argv[1] === 'register-task-definition')!;
    expect(reg.files).toHaveLength(1);
    expect(reg.files[0].mode).toBe('0o600');
    expect(r.stdout).toContain('service kitexample is running 123456789012.dkr.ecr.us-east-1.amazonaws.com/kitexample:t9');
  });

  it('fails with the previous task definition when the circuit breaker rolls back', () => {
    setResponses(sb, [{ match: "deployments[?status=='PRIMARY']", stdout: `${OLD_TD}\tIN_PROGRESS\n` }, ...base]);
    const r = runScript(sb, 'aws/deploy.sh', ['--env', sb.envFile, '--only', 'release', '--image-tag', 't9']);
    expect(r.status).toBe(1);
    expect(r.stderr).toContain(`the primary deployment runs ${OLD_TD}, not ${NEW_TD}`);
    // The way back goes through the script, never a raw update-service.
    expect(r.stderr).toContain(`the previous task definition was ${OLD_TD} (image 123456789012.dkr.ecr.us-east-1.amazonaws.com/kitexample:20261101-000000)`);
    expect(r.stderr).toMatch(/aws\/deploy\.sh --env \S+ --only release,smoke --image-tag 20261101-000000/);
    expect(r.stderr).not.toContain('aws ecs update-service');
  });

  it('refuses a service without this site\'s tag before registering anything', () => {
    setResponses(sb, [{ match: '--include TAGS', stdout: 'None\n' }, ...base]);
    const r = runScript(sb, 'aws/deploy.sh', ['--env', sb.envFile, '--only', 'release', '--image-tag', 't9']);
    expect(r.status).toBe(2);
    expect(r.calls.filter((c) => c.tool === 'aws' && ['register-task-definition', 'update-service'].includes(c.argv[1]))).toEqual([]);
  });
});

describe('--rollback-on-smoke-failure (real mode)', () => {
  /** SMOKE_TIMEOUT_SECONDS=5 keeps the failing smoke check short. */
  const quickSmoke = (extra = '') => editedEnv(sb, (t) => `${t}SMOKE_TIMEOUT_SECONDS=5\n${extra}`);
  const azure = [
    AZ_OWNER,
    { match: 'az acr repository show', stdout: 'sha256:0123\n' },
    { match: '--query name -o tsv', stdout: 'kitexample\n' },
    { match: 'ingress show', stdout: '8080\n' },
    { match: 'properties.template.containers[0].image', stdout: `${PRIOR}\n` },
    { match: '--query properties.latestRevisionName', stdouts: ['kitexample--r2\n', 'kitexample--r3\n'] },
    { match: 'latestReadyRevisionName', stdouts: ['kitexample--r2\n', 'kitexample--r3\n'] },
    { match: 'ingress.fqdn', stdout: 'kitexample.test-env.example.invalid\n' },
  ];

  it('azure: a failed smoke check after the release puts the previous image back and exits 4', () => {
    setResponses(sb, azure);
    const r = runScript(sb, 'azure/deploy.sh', ['--env', quickSmoke(), '--only', 'release,smoke', '--image-tag', 't9', '--rollback-on-smoke-failure']);
    expect(r.status).toBe(4);
    const updates = azCalls(r).filter((c) => c.startsWith('containerapp update'));
    expect(updates).toHaveLength(2);
    expect(updates[0]).toContain('--image kittestacrdoesnotexist.azurecr.io/kitexample:t9 ');
    expect(updates[1]).toContain(`--image ${PRIOR} `);
    expect(updates[1]).toContain('--replace-env-vars PORT=8080 ');
    expect(r.stdout).toContain('FAIL  /');
    expect(r.stderr).toContain(`rolled back: kitexample serves ${PRIOR} again`);
  }, 60_000);

  it('azure: without the flag the release keeps serving, the way back is printed, exit 4', () => {
    setResponses(sb, azure);
    const r = runScript(sb, 'azure/deploy.sh', ['--env', quickSmoke(), '--only', 'release,smoke', '--image-tag', 't9']);
    expect(r.status).toBe(4);
    expect(azCalls(r).filter((c) => c.startsWith('containerapp update'))).toHaveLength(1);
    expect(r.stderr).toContain('--only release,smoke --image-tag 20261101-000000');
    expect(r.stderr).toContain('pass --rollback-on-smoke-failure');
  }, 60_000);

  it('azure: refuses to release when it cannot read the image to roll back to', () => {
    setResponses(sb, [{ match: 'properties.template.containers[0].image', exit: 1, stderr: 'ERROR: Connection aborted.\n' }, ...azure]);
    const r = runScript(sb, 'azure/deploy.sh', ['--env', sb.envFile, '--only', 'release,smoke', '--image-tag', 't9', '--rollback-on-smoke-failure']);
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('a read-only query failed (exit 1)');
    expect(azCalls(r).some((c) => c.startsWith('containerapp update'))).toBe(false);
  });

  it('aws: a failed smoke check after the release points the service back at the previous task definition', () => {
    const NEW_TD = 'arn:aws:ecs:us-east-1:123456789012:task-definition/kitexample:8';
    const OLD_TD = 'arn:aws:ecs:us-east-1:123456789012:task-definition/kitexample:7';
    // The origin check goes through the ALB: give it a name that never resolves.
    const state = path.join(sb.fx, 'aws-state-invalid.env');
    fs.writeFileSync(state, fs.readFileSync(path.join(sb.fx, 'aws-state.env'), 'utf8')
      .replace(/^ALB_DNS=.*$/m, 'ALB_DNS=kit-test-alb.example.invalid'));
    const f = editedEnv(sb, (t) => `${t.replace('AWS_STATE_FILE=aws-state.env', 'AWS_STATE_FILE=aws-state-invalid.env')}SMOKE_TIMEOUT_SECONDS=5\n`);
    setResponses(sb, [
      { match: '--include TAGS', stdout: 'kitexample\n' },
      { match: 'describe-task-definition', stdout: '123456789012.dkr.ecr.us-east-1.amazonaws.com/kitexample:20261101-000000\n' },
      { match: 'ecr describe-images', stdout: 'sha256:0123\n' },
      { match: "services[?status=='ACTIVE'].serviceName", stdout: 'kitexample\n' },
      { match: '--query services[0].taskDefinition', stdout: `${OLD_TD}\n` },
      { match: 'secretsmanager describe-secret', stdout: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:nsc/kitexample-AbCdEf\n' },
      { match: 'register-task-definition', stdout: `${NEW_TD}\n` },
      { match: "deployments[?status=='PRIMARY']", stdouts: [`${NEW_TD}\tCOMPLETED\n`, `${OLD_TD}\tCOMPLETED\n`] },
    ]);
    const r = runScript(sb, 'aws/deploy.sh', ['--env', f, '--only', 'release,smoke', '--image-tag', 't9', '--rollback-on-smoke-failure']);
    expect(r.status).toBe(4);
    const updates = r.calls.filter((c) => c.tool === 'aws' && c.argv[1] === 'update-service').map((c) => c.argv.join(' '));
    expect(updates).toHaveLength(2);
    expect(updates[0]).toContain(`--task-definition ${NEW_TD} `);
    expect(updates[1]).toContain(`--task-definition ${OLD_TD} `);
    expect(r.stderr).toContain(`rolled back: kitexample runs ${OLD_TD} again`);
  }, 60_000);
});

describe('existence probes (real mode)', () => {
  const missingApp = { match: '--query name -o tsv', exit: 1,
    stderr: "ERROR: (ResourceNotFound) The Resource 'Microsoft.App/containerApps/kitexample' under resource group 'kit-test-rg-does-not-exist' was not found.\n" };
  const digest = { match: 'az acr repository show', stdout: 'sha256:0123\n' };
  const fqdn = { match: 'ingress.fqdn', stdout: 'kitexample.test-env.example.invalid\n' };

  it('read a not-found error as "absent": provision creates the app', () => {
    setResponses(sb, [missingApp, digest, fqdn]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'app', '--image-tag', 't1']);
    expect(r.status).toBe(0);
    expect(azCalls(r).filter((c) => c.startsWith('containerapp create'))).toHaveLength(1);
  });

  it('stop on any other error rather than create over a live app', () => {
    setResponses(sb, [{ match: '--query name -o tsv', exit: 1, stderr: 'ERROR: HTTPSConnectionPool(host=management.azure.com): Read timed out.\n' }, digest, fqdn]);
    const r = runScript(sb, 'azure/provision.sh', ['--env', sb.envFile, 'app', '--image-tag', 't1']);
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('a read-only query failed (exit 1)');
    expect(r.stderr).toContain('Read timed out');
    expect(azCalls(r).filter((c) => /^containerapp (create|update|ingress update)/.test(c))).toEqual([]);
  });

  it('aws: a *NotFound* exception means absent, anything else stops', () => {
    const notFound = { match: 'secretsmanager describe-secret', exit: 254,
      stderr: "An error occurred (ResourceNotFoundException) when calling the DescribeSecret operation: Secrets Manager can't find the specified secret.\n" };
    setResponses(sb, [notFound]);
    const a = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, 'secrets']);
    expect(a.status).toBe(0);
    expect(a.calls.filter((c) => c.argv[1] === 'create-secret')).toHaveLength(1);
    setResponses(sb, [{ ...notFound, stderr: 'Could not connect to the endpoint URL: "https://secretsmanager.us-east-1.amazonaws.com/"\n' }]);
    const b = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, 'secrets']);
    expect(b.status).toBe(1);
    expect(b.calls.filter((c) => ['create-secret', 'put-secret-value'].includes(c.argv[1]))).toEqual([]);
  });
});
