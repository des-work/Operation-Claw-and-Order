// Called by the isolated PostgreSQL rehearsal. Never point this at live databases.
import assert from 'node:assert/strict';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const dbUrl = new URL(process.env.DATABASE_URL || '');
if (!dbUrl.pathname.startsWith('/claw_dashboard_rehearsal_')) throw new Error('A fresh claw_dashboard_rehearsal_* database is required');
const root = process.env.CLAW_DASHBOARD_DIR;
if (!root) throw new Error('Set CLAW_DASHBOARD_DIR to the dashboard checkout');
const moduleAt = name => import(pathToFileURL(path.join(root, 'server', name)).href);
const db = await moduleAt('db.js');
const roster = await moduleAt('enrollment-sync.js');
const scoring = await moduleAt('backend-sync.js');
const { attachTopology } = await moduleAt('topology.js');
await db.migrateDb();
await db.migrateDb();
const { rows: existing } = await db.getPool().query('SELECT COUNT(*) AS count FROM events');
assert.equal(Number(existing[0].count), 0, 'Rehearsal dashboard must start empty');
const settings = { baseUrl: process.env.BACKEND_EXPORT_URL, secret: process.env.BACKEND_EXPORT_SECRET };
await roster.syncEnrollmentOnce(settings);
await roster.restoreEnrollmentInventory();
const inventory = roster.getEnrollmentInventory();
assert.equal(inventory.length, 3);
assert.deepEqual([...new Set(inventory.map(team => team.class_id))].sort(), ['A', 'B']);
const delivery = await scoring.syncBackendOnce(settings);
assert.equal(delivery.imported, 1);
assert.equal(delivery.pendingEvents, 0);
const teams = attachTopology((await db.getTeamStatuses()).map(item => ({ ...item, status: 'unknown' })),
  roster.mergeEnrollmentInventory([], inventory), await db.getMachineStatuses());
assert.equal(teams.length, 3);
assert.ok(teams.every(team => !team.machines.length));
assert.ok(!JSON.stringify(teams).includes('@college.example'));
assert.ok(!JSON.stringify(teams).includes('private idea'));
const repeat = await scoring.syncBackendOnce(settings);
assert.equal(repeat.imported, 0);
console.log(JSON.stringify({ teams: teams.length, classes: 2, imported_events: delivery.imported,
  duplicate_events_on_replay: repeat.imported, pending_events: repeat.pendingEvents,
  student_identity_fields_exposed: false, invented_machine_reports: false }));
await db.getPool().end();
