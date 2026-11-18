import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import express from 'express';
import request from 'supertest';
import {
  KIT_CLIENT_DIST_FILE, kitClientBundleFromSource, kitClientHandler, kitClientInlineScript, kitClientScriptTag,
  loadKitClientBundle,
} from '../../server/src/http/clientAsset';

const JS = '"use strict";(()=>{document.title="x";})();';

describe('kit client bundle', () => {
  it('is addressed by its content hash', () => {
    const b = kitClientBundleFromSource(JS);
    const sha = crypto.createHash('sha256').update(JS).digest('hex');
    expect(b).toEqual({ js: JS, bytes: Buffer.byteLength(JS), sha256: sha, path: `/kit/client.${sha.slice(0, 12)}.js` });
    expect(kitClientBundleFromSource(`${JS} `).path).not.toBe(b.path);
    expect(() => kitClientBundleFromSource('  ')).toThrow(/empty/);
  });

  it('loads the built file, and says how to build it when it is missing', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'kit-client-'));
    try {
      const file = path.join(dir, 'kit-client.js');
      fs.writeFileSync(file, JS);
      expect(loadKitClientBundle(file).js).toBe(JS);
      expect(() => loadKitClientBundle(path.join(dir, 'missing.js'))).toThrow(/build-client\.mjs/);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
    expect(KIT_CLIENT_DIST_FILE).toBe('client/dist/kit-client.js');
  });

  it('is served immutable, with an ETag and 304s', async () => {
    const b = kitClientBundleFromSource(JS);
    const app = express();
    app.get(b.path, kitClientHandler(b));
    const res = await request(app).get(b.path).expect(200);
    expect(res.text).toBe(JS);
    expect(res.headers['content-type']).toBe('application/javascript; charset=utf-8');
    expect(res.headers['cache-control']).toBe('public, max-age=31536000, immutable');
    expect(res.headers['x-content-type-options']).toBe('nosniff');
    await request(app).get(b.path).set('If-None-Match', res.headers.etag as string).expect(304);
  });

  it('renders a deferred script tag, or the inlined bundle with </script> escaped', () => {
    const b = kitClientBundleFromSource(JS);
    expect(kitClientScriptTag(b)).toBe(`<script defer src="${b.path}"></script>`);
    const inline = kitClientInlineScript({ js: 'var s="</script><b>";var t="</SCRIPT>";' });
    expect(inline).toBe('<script>var s="<\\/script><b>";var t="<\\/SCRIPT>";</script>');
    expect(inline.toLowerCase().indexOf('</script')).toBe(inline.length - '</script>'.length);
  });
});
