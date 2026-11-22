/**
 * aws/{provision,deploy,teardown}.sh under --dry-run against the sample
 * deploy.env and a fake ~/.aws-deploy/state.env: the aws / docker / psql command
 * lines, the task definition, and the guards. No stub may be called.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  cloudCalls, dryCommands, expectNoSecrets, makeSandbox, oneCommand, removeSandbox, runScript, shownFile, sqlBlocks,
  type RunResult, type Sandbox,
} from './harness';

let sb: Sandbox;
beforeAll(() => {
  sb = makeSandbox();
});
afterAll(() => removeSandbox(sb));

function dry(script: string, args: string[], env: Record<string, string> = {}): RunResult {
  const r = runScript(sb, script, ['--env', sb.envFile, '--dry-run', ...args], env);
  expect(cloudCalls(r.calls)).toEqual([]);
  expectNoSecrets(r.all);
  return r;
}

const ALB = 'arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/kit-test-alb/0123456789abcdef';

describe('aws/provision.sh --dry-run all (fresh)', () => {
  let r: RunResult;
  beforeAll(() => {
    r = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'all']);
  });

  it('runs check, database, secrets, domain, app (domain first: ECS needs a listener)', () => {
    expect(r.status).toBe(0);
    expect(cloudCalls(r.calls)).toEqual([]);
    expectNoSecrets(r.all);
    const at = ['== check ==', '== database ==', '== secrets ==', '== domain ==', '== app =='].map((h) => r.stdout.indexOf(h));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
  });

  it('applies the same role SQL on the RDS endpoint', () => {
    const [cluster] = sqlBlocks(r.stdout);
    expect(r.stdout).toContain("psql 'host=kit-test-db-does-not-exist.abcdefghij.us-east-1.rds.amazonaws.com port=5432 dbname=postgres user=kitadmin sslmode=require");
    expect(r.stdout).toContain('PGPASSWORD=<env:KIT_TEST_RDS_ADMIN_PASSWORD>');
    expect(cluster).toContain('CONNECTION LIMIT 20');
    expect(cluster).toContain(`ALTER ROLE "kitexample_app" SET statement_timeout = '15s';`);
    expect(cluster).not.toMatch(/ALTER DATABASE "kitexample" WITH CONNECTION LIMIT/);
  });

  it('sets DB_CONNECTION_LIMIT on the database exactly as azure/provision.sh does, and refuses one below the roles\' sum', () => {
    const f = path.join(sb.fx, 'aws-dblimit.env');
    fs.writeFileSync(f, `${fs.readFileSync(sb.envFile, 'utf8')}DB_CONNECTION_LIMIT=40\n`);
    const aws = runScript(sb, 'aws/provision.sh', ['--env', f, '--dry-run', 'database']);
    const az = runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'database']);
    expect([aws.status, az.status]).toEqual([0, 0]);
    expect(sqlBlocks(aws.stdout)[0]).toContain('ALTER DATABASE "kitexample" WITH CONNECTION LIMIT 40;');
    expect(sqlBlocks(aws.stdout)).toEqual(sqlBlocks(az.stdout));
    expect(aws.stdout).toContain('connection limits: roles owner 3, app 20, agents 10, publisher 2 (sum 35); database 40');
    fs.writeFileSync(f, `${fs.readFileSync(sb.envFile, 'utf8')}DB_CONNECTION_LIMIT=20\n`);
    const low = runScript(sb, 'aws/provision.sh', ['--env', f, '--dry-run', 'check']);
    expect(low.status).toBe(2);
    expect(low.stderr).toContain('DB_CONNECTION_LIMIT=20 is below the sum of the four role limits (35)');
    expect(low.calls).toEqual([]);
  });

  it('stores the container secrets in Secrets Manager from a 0600 file', () => {
    expect(r.stdout).toContain('[dry-run] (file <work-dir>/app-secrets.json, 0600) a JSON object with the keys: database-url');
    expect(oneCommand(r.stdout, 'aws secretsmanager create-secret ')).toContain(
      "--name nsc/kitexample --description 'affiliate-kit container secrets for kitexample' --secret-string 'file://<work-dir>/app-secrets.json'");
  });

  it('requests a DNS-validated certificate, prints the records, and routes the hostnames through the ALB', () => {
    const req = oneCommand(r.stdout, 'aws acm request-certificate ');
    expect(req).toContain('--domain-name kit-example.test --validation-method DNS --idempotency-token kit_kitexample');
    expect(req).toContain('--subject-alternative-names www.kit-example.test');
    expect(r.stdout).toMatch(/ALIAS +kit-example\.test +kit-test-alb-1234567890\.us-east-1\.elb\.amazonaws\.com/);
    expect(r.stdout).toMatch(/CNAME +www\.kit-example\.test +kit-test-alb-1234567890\.us-east-1\.elb\.amazonaws\.com/);
    expect(oneCommand(r.stdout, 'aws elbv2 create-target-group ')).toContain(
      '--name tg-kitexample --protocol HTTP --port 8080 --vpc-id vpc-0kittest00000000 --target-type ip --health-check-protocol HTTP --health-check-path /health');
    const listeners = dryCommands(r.stdout, 'aws elbv2 create-listener ');
    expect(listeners).toHaveLength(2);
    expect(listeners[0]).toContain(`--load-balancer-arn ${ALB} --protocol HTTPS --port 443 --ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06 --certificates 'CertificateArn=<certificate-arn>'`);
    expect(listeners[1]).toContain('"Type":"redirect","RedirectConfig":{"Protocol":"HTTPS","Port":"443","StatusCode":"HTTP_301"}');
    const rule = oneCommand(r.stdout, 'aws elbv2 create-rule ');
    expect(rule).toContain(`--conditions '[{"Field":"host-header","HostHeaderConfig":{"Values":["kit-example.test","www.kit-example.test"]}}]'`);
    expect(rule).toContain(`--actions '[{"Type":"forward","TargetGroupArn":"<target-group-arn>"}]'`);
  });

  it('runs the service at 512 CPU units / 1024 MiB, 1..3 tasks, secrets by ARN, circuit breaker on', () => {
    const td = JSON.parse(shownFile(r.stdout, 'taskdef.json'));
    expect(td).toMatchObject({ family: 'kitexample', cpu: '512', memory: '1024', networkMode: 'awsvpc',
      executionRoleArn: 'arn:aws:iam::123456789012:role/kitTestExecRoleDoesNotExist' });
    const c = td.containerDefinitions[0];
    expect(c.image).toBe('123456789012.dkr.ecr.us-east-1.amazonaws.com/kitexample:latest');
    expect(c.environment).toEqual([
      { name: 'PORT', value: '8080' }, { name: 'NODE_ENV', value: 'production' }, { name: 'PG_POOL_MAX', value: '5' },
      { name: 'SITE_ID', value: 'kit-example' }, { name: 'AI_TRAFFIC_EXCLUDE_IPS', value: '198.51.100.7' },
    ]);
    expect(c.secrets).toEqual([{ name: 'DATABASE_URL',
      valueFrom: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:nsc/kitexample-XXXXXX:database-url::' }]);
    const svc = oneCommand(r.stdout, 'aws ecs create-service ');
    for (const part of [
      '--cluster kit-test-cluster-does-not-exist --service-name kitexample', '--desired-count 1 --launch-type FARGATE',
      "'awsvpcConfiguration={subnets=[subnet-0kitpriv00000000,subnet-0kitpriv00000001],securityGroups=[sg-0kitecs0000000000],assignPublicIp=DISABLED}'",
      "--load-balancers 'targetGroupArn=<target-group>,containerName=kitexample,containerPort=8080'",
      "'deploymentCircuitBreaker={enable=true,rollback=true},maximumPercent=200,minimumHealthyPercent=100'",
    ]) expect(svc).toContain(part);
    expect(oneCommand(r.stdout, 'aws application-autoscaling register-scalable-target ')).toContain(
      '--resource-id service/kit-test-cluster-does-not-exist/kitexample --min-capacity 1 --max-capacity 3');
    expect(JSON.parse(shownFile(r.stdout, 'scaling-policy.json')).PredefinedMetricSpecification.PredefinedMetricType)
      .toBe('ECSServiceAverageCPUUtilization');
  });
});

describe('aws/provision.sh guards', () => {
  it('app refuses to start before the target group sits behind a listener', () => {
    const r = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'app'], { KIT_DEPLOY_DRY_MISSING: 'target-group' });
    expect(r.status).toBe(3);
    expect(r.stderr).toContain('run the domain phase first');
  });

  it('refuses a CPU with no Fargate size', () => {
    const f = path.join(sb.fx, 'deploy-cpu.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8').replace('CPU=0.5', 'CPU=0.75').replace('MEMORY=1Gi', 'MEMORY=1.5Gi'));
    const r = runScript(sb, 'aws/provision.sh', ['--env', f, '--dry-run', 'check']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('has no Fargate size');
    // Azure takes the same size.
    expect(runScript(sb, 'azure/provision.sh', ['--env', f, '--dry-run', 'check']).status).toBe(0);
  });

  it('a real run without the state file stops before any aws call', () => {
    const f = path.join(sb.fx, 'deploy-nostate.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8').replace('AWS_STATE_FILE=aws-state.env', 'AWS_STATE_FILE=missing.env'));
    const r = runScript(sb, 'aws/provision.sh', ['--env', f, 'check']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('run reusable-agents/install/deploy-aws.sh provision first');
    expect(r.calls).toEqual([]);
  });
});

describe('aws/deploy.sh --dry-run', () => {
  let r: RunResult;
  beforeAll(() => {
    r = runScript(sb, 'aws/deploy.sh', ['--env', sb.envFile, '--dry-run', '--image-tag', '20261117-120000']);
  });

  it('runs every stage, calls nothing, leaks nothing', () => {
    expect(r.status).toBe(0);
    expect(cloudCalls(r.calls)).toEqual([]);
    expectNoSecrets(r.all);
  });

  it('logs in to ECR over stdin and pushes the tag and latest', () => {
    expect(r.stdout).toContain('[dry-run] aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com');
    expect(dryCommands(r.stdout, 'docker push ')).toEqual([
      '[dry-run] docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/kitexample:20261117-120000',
      '[dry-run] docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/kitexample:latest',
    ]);
  });

  it('registers a task definition with the new image, updates the service and waits for the rollout', () => {
    const td = JSON.parse(shownFile(r.stdout, 'taskdef.json'));
    expect(td.containerDefinitions[0].image).toBe('123456789012.dkr.ecr.us-east-1.amazonaws.com/kitexample:20261117-120000');
    expect(oneCommand(r.stdout, 'aws ecs update-service ')).toContain("--task-definition '<taskdef-arn>' --deployment-configuration");
    expect(oneCommand(r.stdout, 'aws application-autoscaling register-scalable-target ')).toContain('--min-capacity 1 --max-capacity 3');
    expect(r.stdout).toContain('(wait up to 600s) <taskdef-arn> becomes the completed primary deployment');
    expect(r.stdout).toContain("--query 'services[0].deployments[?status=='\\''PRIMARY'\\''] | [0].[taskDefinition, rolloutState]'");
  });

  it('smoke-checks the custom domain through the ALB, then the public domain', () => {
    const smokes = dryCommands(r.stdout).filter((l) => l.includes('deploytool.py smoke'));
    expect(smokes).toHaveLength(2);
    expect(smokes[0]).toContain('--label origin --url https://kit-example.test');
    expect(smokes[0]).toContain('--connect-to kit-test-alb-1234567890.us-east-1.elb.amazonaws.com:443');
    expect(smokes[1]).toContain('--optional');
  });

  it('exits 3 when the service does not exist', () => {
    const s = runScript(sb, 'aws/deploy.sh', ['--env', sb.envFile, '--dry-run', '--only', 'release', '--image-tag', 't'],
      { KIT_DEPLOY_DRY_MISSING: 'service' });
    expect(s.status).toBe(3);
  });
});

describe('aws/teardown.sh', () => {
  it('removes only the site, in dependency order', () => {
    const r = dry('aws/teardown.sh', ['--with-certificates', '--with-images', '--with-database']);
    expect(r.status).toBe(0);
    const cmds = dryCommands(r.stdout).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'))
      .map((l) => l.split(' ').slice(1, 4).join(' '));
    expect(cmds).toEqual([
      'aws ecs update-service', 'aws ecs delete-service', 'aws application-autoscaling deregister-scalable-target',
      'aws ecs deregister-task-definition', 'aws elbv2 delete-rule', 'aws elbv2 remove-listener-certificates',
      'aws elbv2 delete-target-group', 'aws secretsmanager delete-secret', 'aws acm delete-certificate',
      'aws ecr delete-repository', "PGPASSWORD=<env:KIT_TEST_RDS_ADMIN_PASSWORD> psql 'host=kit-test-db-does-not-exist.abcdefghij.us-east-1.rds.amazonaws.com",
    ]);
    expect(r.stdout).toContain('--recovery-window-in-days 7');
    expect(r.all).not.toMatch(/delete-load-balancer|delete-cluster|delete-db-instance|delete-listener /);
  });

  it('a real run needs --confirm', () => {
    const r = runScript(sb, 'aws/teardown.sh', ['--env', sb.envFile]);
    expect(r.status).toBe(2);
    expect(r.calls).toEqual([]);
  });
});

describe('aws/deploy.sh: the same stage selection, gate policy and rollback contract as Azure', () => {
  const run = (args: string[], env: Record<string, string> = {}) =>
    runScript(sb, 'aws/deploy.sh', ['--env', sb.envFile, '--dry-run', ...args], env);

  it('an empty stage list is an error', () => {
    for (const args of [['--only', ''], ['--only=']]) {
      const r = run([...args, '--image-tag', 't1']);
      expect(r.status).toBe(2);
      expect(r.stderr).toContain('--only needs at least one stage');
      expect(r.calls).toEqual([]);
    }
  });

  it('refuses to build without the gate unless --gated-by or --skip-tests', () => {
    const r = run(['--only', 'build,push,release', '--image-tag', 't1']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('untested code does not ship');
    const g = dry('aws/deploy.sh', ['--only', 'build', '--gated-by=deployer', '--image-tag', 't1']);
    expect(g.status).toBe(0);
    expect(g.stdout).toContain('gate: run by deployer');
  });

  it('refuses to release to a service without this site\'s tag', () => {
    const ok = dry('aws/deploy.sh', ['--only', 'release', '--image-tag', 't1']);
    expect(ok.stdout).toContain("--include TAGS --query 'services[?status=='\\''ACTIVE'\\''] | [0].tags[?key=='\\''affiliate-kit-app'\\''].value | [0]'");
    const r = run(['--only', 'release', '--image-tag', 't1'], { KIT_DEPLOY_DRY_MISSING: 'service-owner-tag' });
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('ECS service kitexample on kit-test-cluster-does-not-exist exists but was not created by the kit for kitexample');
    expect(dryCommands(r.stdout, 'aws ecs update-service')).toEqual([]);
    expect(dryCommands(r.stdout, 'aws ecs register-task-definition')).toEqual([]);
  });

  it('--rollback-on-smoke-failure reads the running task definition and its image before it releases', () => {
    const r = dry('aws/deploy.sh', ['--only', 'release,smoke', '--rollback-on-smoke-failure', '--image-tag', 't4']);
    expect(r.status).toBe(0);
    const prior = r.stdout.indexOf("--query 'services[0].taskDefinition' --output text");
    expect(prior).toBeGreaterThan(0);
    expect(r.stdout).toContain("(probe) aws ecs describe-task-definition --region us-east-1 --task-definition '<prior-taskdef>' --query 'taskDefinition.containerDefinitions[0].image' --output text");
    expect(prior).toBeLessThan(r.stdout.indexOf('[dry-run] aws ecs update-service'));
  });

  it('requires AWS_SECRET_ID (no fleet default in code)', () => {
    const f = path.join(sb.fx, 'deploy-nosecretid.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8').replace(/^AWS_SECRET_ID=.*\n/m, ''));
    const r = runScript(sb, 'aws/deploy.sh', ['--env', f, '--dry-run', '--smoke-only', '--image-tag', 't1']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('set AWS_SECRET_ID');
  });
});

describe('aws/provision.sh: ownership', () => {
  it('refuses to overwrite an existing secret without this site\'s tag; --adopt-existing tags it first', () => {
    const env = { KIT_DEPLOY_DRY_EXISTING: 'secret-arn', KIT_DEPLOY_DRY_MISSING: 'secret-owner-tag' };
    const r = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'secrets'], env);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('Secrets Manager secret nsc/kitexample exists but was not created by the kit for kitexample');
    expect(dryCommands(r.stdout, 'aws secretsmanager put-secret-value')).toEqual([]);
    const a = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'secrets', '--adopt-existing'], env);
    expect(a.status).toBe(0);
    const tag = oneCommand(a.stdout, 'aws secretsmanager tag-resource ');
    expect(tag).toContain('--secret-id nsc/kitexample --tags Key=affiliate-kit-app,Value=kitexample');
    expect(a.stdout.indexOf(tag)).toBeLessThan(a.stdout.indexOf('[dry-run] aws secretsmanager put-secret-value'));
  });

  it('refuses a target group or a service that is not this site\'s before the app phase changes anything', () => {
    const tg = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'app'],
      { KIT_DEPLOY_DRY_EXISTING: 'target-group', KIT_DEPLOY_DRY_MISSING: 'tg-owner-tag' });
    expect(tg.status).toBe(2);
    expect(tg.stderr).toContain('target group tg-kitexample exists but was not created by the kit');
    expect(dryCommands(tg.stdout).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'))).toEqual([]);
    const svc = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'app'],
      { KIT_DEPLOY_DRY_EXISTING: 'target-group,service', KIT_DEPLOY_DRY_MISSING: 'service-owner-tag' });
    expect(svc.status).toBe(2);
    expect(dryCommands(svc.stdout).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'))).toEqual([]);
  });

  it('the database phase needs the admin login in deploy.env', () => {
    const f = path.join(sb.fx, 'deploy-noadmin.env');
    fs.writeFileSync(f, fs.readFileSync(sb.envFile, 'utf8').replace(/^AWS_DB_ADMIN_USER=.*\n/m, ''));
    const r = runScript(sb, 'aws/provision.sh', ['--env', f, '--dry-run', 'database']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('set AWS_DB_ADMIN_USER');
  });
});

describe('aws/provision.sh: the certificate, the host-header rule and the ECR repository are this site\'s too', () => {
  const changes = (out: string) => dryCommands(out).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'));

  it('refuses an existing certificate for SITE_DOMAIN without this site\'s tag; --adopt-existing tags it first', () => {
    const env = { KIT_DEPLOY_DRY_EXISTING: 'certificate', KIT_DEPLOY_DRY_MISSING: 'cert-owner-tag' };
    const r = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'domain'], env);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain("ACM certificate <certificate> for kit-example.test exists but was not created by the kit for kitexample");
    expect(r.stdout).toContain("(probe) aws acm list-tags-for-certificate --region us-east-1 --certificate-arn '<certificate>' --query 'Tags[?Key=='\\''affiliate-kit-app'\\''].Value | [0]' --output text");
    expect(changes(r.stdout)).toEqual([]);
    const a = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'domain', '--adopt-existing'], env);
    expect(a.status).toBe(0);
    const tag = oneCommand(a.stdout, 'aws acm add-tags-to-certificate ');
    expect(tag).toContain("--certificate-arn '<certificate>' --tags Key=affiliate-kit-app,Value=kitexample");
    // Tagged before the certificate goes onto a listener.
    expect(a.stdout.indexOf('[dry-run] aws elbv2 create-listener')).toBeGreaterThan(a.stdout.indexOf(tag));
  });

  it('refuses another site\'s host-header rule on the shared listener before modify-rule could re-point it', () => {
    const env = { KIT_DEPLOY_DRY_EXISTING: 'listener-443,host-rule', KIT_DEPLOY_DRY_MISSING: 'rule-owner-tag' };
    const r = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'domain'], env);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain("the HTTPS listener's host-header rule <host-rule> for kit-example.test exists but was not created by the kit");
    expect(r.stdout).toContain("(probe) aws elbv2 describe-tags --region us-east-1 --resource-arns '<host-rule>' --query 'TagDescriptions[0].Tags[?Key=='\\''affiliate-kit-app'\\''].Value | [0]' --output text");
    expect(changes(r.stdout)).toEqual([]);
    const a = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'domain', '--adopt-existing'], env);
    expect(a.status).toBe(0);
    const tag = oneCommand(a.stdout, 'aws elbv2 add-tags ');
    expect(tag).toContain("--resource-arns '<host-rule>' --tags Key=affiliate-kit-app,Value=kitexample");
    expect(a.stdout.indexOf(tag)).toBeLessThan(a.stdout.indexOf('[dry-run] aws elbv2 modify-rule'));
  });

  it('refuses an existing ECR repository without this site\'s tag in the app phase; --adopt-existing tags it first', () => {
    const env = { KIT_DEPLOY_DRY_EXISTING: 'target-group,ecr-repository', KIT_DEPLOY_DRY_MISSING: 'ecr-owner-tag' };
    const r = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'app'], env);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('ECR repository kitexample exists but was not created by the kit for kitexample');
    // ListTagsForResource answers {"tags": [{"Key", "Value"}]}.
    expect(r.stdout).toContain("(probe) aws ecr list-tags-for-resource --region us-east-1 --resource-arn '<ecr-repository-arn>' --query 'tags[?Key=='\\''affiliate-kit-app'\\''].Value | [0]' --output text");
    expect(changes(r.stdout)).toEqual([]);
    const a = runScript(sb, 'aws/provision.sh', ['--env', sb.envFile, '--dry-run', 'app', '--adopt-existing'], env);
    expect(a.status).toBe(0);
    expect(oneCommand(a.stdout, 'aws ecr tag-resource ')).toContain("--resource-arn '<ecr-repository-arn>' --tags Key=affiliate-kit-app,Value=kitexample");
  });

  it('a fresh provision checks none of them (nothing exists to belong to another site)', () => {
    const r = dry('aws/provision.sh', ['domain', 'app']);
    expect(r.status).toBe(0);
    expect(r.stdout).not.toMatch(/list-tags-for-certificate|ecr list-tags-for-resource|add-tags-to-certificate|ecr tag-resource/);
  });
});

describe('aws/teardown.sh: ownership and confirmation', () => {
  it.each([['service-owner-tag'], ['tg-owner-tag'], ['secret-owner-tag'], ['cert-owner-tag'], ['rule-owner-tag'], ['ecr-owner-tag']])(
    'refuses when %s is missing, before deleting anything', (tag) => {
      const r = runScript(sb, 'aws/teardown.sh', ['--env', sb.envFile, '--dry-run', '--with-certificates', '--with-images'],
        { KIT_DEPLOY_DRY_MISSING: tag });
      expect(r.status).toBe(2);
      expect(r.stderr).toContain('was not created by the kit for kitexample');
      expect(dryCommands(r.stdout).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'))).toEqual([]);
    });

  it('the certificate and the rule are checked on every teardown (the rule is deleted and the certificate detached), the repository only with --with-images', () => {
    for (const tag of ['cert-owner-tag', 'rule-owner-tag']) {
      const r = runScript(sb, 'aws/teardown.sh', ['--env', sb.envFile, '--dry-run'], { KIT_DEPLOY_DRY_MISSING: tag });
      expect([tag, r.status]).toEqual([tag, 2]);
      expect(dryCommands(r.stdout).filter((l) => !l.includes('(probe)') && !l.includes('affiliate-kit deploy'))).toEqual([]);
    }
    const noImages = runScript(sb, 'aws/teardown.sh', ['--env', sb.envFile, '--dry-run'], { KIT_DEPLOY_DRY_MISSING: 'ecr-owner-tag' });
    expect(noImages.status).toBe(0);
    expect(noImages.stdout).not.toContain('ecr list-tags-for-resource');
    expect(dryCommands(noImages.stdout, 'aws ecr delete-repository')).toEqual([]);
  });

  it('a teardown where the service, target group and secret are gone still proves the certificate and rule are this site\'s', () => {
    const r = runScript(sb, 'aws/teardown.sh', ['--env', sb.envFile, '--dry-run', '--with-certificates', '--with-images'],
      { KIT_DEPLOY_DRY_MISSING: 'service,target-group,secret,cert-owner-tag' });
    expect(r.status).toBe(2);
    expect(dryCommands(r.stdout, 'aws elbv2 delete-rule')).toEqual([]);
    expect(dryCommands(r.stdout, 'aws elbv2 remove-listener-certificates')).toEqual([]);
    expect(dryCommands(r.stdout, 'aws acm delete-certificate')).toEqual([]);
    expect(dryCommands(r.stdout, 'aws ecr delete-repository')).toEqual([]);
  });

  it('a real --with-database run needs --confirm-database DB_NAME', () => {
    const r = runScript(sb, 'aws/teardown.sh', ['--env', sb.envFile, '--confirm', 'kitexample', '--with-database']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('pass --confirm-database kitexample');
    expect(r.calls).toEqual([]);
  });
});

describe('--help', () => {
  it.each(['aws/provision.sh', 'aws/deploy.sh', 'aws/teardown.sh'])('%s prints its usage', (s) => {
    const r = runScript(sb, s, ['--help']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain(s);
  });
});
