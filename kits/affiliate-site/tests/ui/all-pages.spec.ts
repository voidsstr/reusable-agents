import { registerAllPagesSuite } from './all-pages.engine';
import { loadGateConfig } from './lib/gateConfig';

// Every public page family of the site under test, audited the way a reader
// sees it (all-pages.engine.ts). The families come from the site's gate config
// (KIT_GATE_CONFIG, its tests/ui/gate.config.ts).
registerAllPagesSuite(loadGateConfig());
