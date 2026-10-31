const assert = require('node:assert/strict')
const test = require('node:test')
const Model = require('../Model.js')

test('folder rows preserve legacy depth and merge duplicates using the deepest scan', () => {
  assert.deepEqual(Model.parseFolderRows([' ~/Projects ', {path:'~/Projects',depth:3}, {path:'/tmp/repo',depth:0}]), {
    folders: [{path:'~/Projects',depth:3}, {path:'/tmp/repo',depth:0}], error: ''
  })
  assert.deepEqual(Model.parseFolderRows([]).folders, [])
  assert.deepEqual(Model.repositoryFolders({repositoryFolders:[]}), [])
  assert.deepEqual(Model.repositoryFolders({}), [])
})

test('folder rows reject empty paths, relative paths, invalid depths and oversized lists', () => {
  for (const path of ['', 'Projects', '~someone/Projects'])
    assert.match(Model.parseFolderRows([{path,depth:2}]).error, /absolute path/)
  for (const depth of [-1,6,1.5,'2',null,true])
    assert.match(Model.parseFolderRows([{path:'/tmp',depth}]).error, /scan depth/)
  assert.match(Model.parseFolderRows(Array(33).fill('/tmp')).error, /32/)
})

test('unavailable repositories stay visible and cannot reuse old safety checks', () => {
  const before = {...Model.defaultStatus(), repos:[{path:'/repo', label:'repo', complete:true,
    ahead:1, behind:2, dirtyCount:3, affected:true}]}
  const after = Model.unavailableStatus(before, 'Scan limit reached')
  assert.equal(after.ok, false)
  assert.equal(after.totals.unavailableRepos, 1)
  assert.equal(after.repos[0].affected, true)
  assert.equal(after.repos[0].complete, false)
  assert.equal(after.repos[0].behind, 0)
  assert.match(Model.repositoryMeta(after.repos[0]), /Status unavailable.*Scan limit/)
})

test('status parser rejects oversized messages, repository lists and invalid rows', () => {
  assert.equal(Model.parseStatus('x'.repeat(8 * 1024 * 1024 + 1)).ok, false)
  assert.equal(Model.parseStatus(JSON.stringify({ok:true, repos:Array(257).fill({})})).ok, false)
  assert.equal(Model.parseStatus(JSON.stringify({ok:true, repos:[{complete:true}]})).ok, false)
  const row = {path:'/repo', name:'repo', owner:'projects', label:'repo', branch:'main', upstream:'',
    ahead:0, behind:0, dirtyCount:0, complete:false, affected:true, error:'Timeout'}
  assert.equal(Model.parseStatus(JSON.stringify({ok:true, repos:[row]})).ok, true)
  assert.equal(Model.parseStatus(JSON.stringify({ok:true, repos:[{...row, dirtyCount:-1}]})).ok, false)
})

test('watcher retries back off from 30 seconds to 30 minutes and reset after a stable run', () => {
  let current = Model.WATCH_RETRY_MIN_MS
  const delays = []
  for (let i = 0; i < 9; i++) { const r = Model.watchRetry(current, 1000); delays.push(r.delay); current = r.next }
  assert.deepEqual(delays.map(d => d / 1000), [30, 60, 120, 240, 480, 960, 1800, 1800, 1800])
  assert.equal(Model.watchRetry(current, 11 * 60 * 1000).delay, 30000)
})

test('status rows accept an optional syncError string and reject other types', () => {
  const row = {path:'/repo', name:'repo', owner:'projects', label:'repo', branch:'main', upstream:'',
    ahead:0, behind:0, dirtyCount:0, complete:true, affected:false, error:''}
  assert.equal(Model.parseStatus(JSON.stringify({ok:true, repos:[row]})).repos[0].syncError, '')
  assert.equal(Model.parseStatus(JSON.stringify({ok:true, repos:[{...row, syncError:'x'}]})).ok, true)
  assert.equal(Model.parseStatus(JSON.stringify({ok:true, repos:[{...row, syncError:5}]})).ok, false)
})

test('clean repositories whose last fetch failed stay listed with their error', () => {
  const clean = {path:'/a', affected:false, syncError:''}
  const failed = {path:'/b', affected:false, syncError:'Permission denied (publickey).'}
  const dirty = {path:'/c', affected:true, syncError:''}
  assert.deepEqual(Model.displayedRepos([clean, failed, dirty]).map(r => r.path), ['/b', '/c'])
  assert.deepEqual(Model.displayedRepos(undefined), [])
})

test('per-repository trust: entries carry path and identity', () => {
  const a = {path: '/a', identity: '1:2:3'}, b = {path: '/b', identity: '4:5:6'}
  assert.deepEqual(Model.trustedRepositories({trustedRepositories: [a, '/legacy', {path: 'rel', identity: 'x'}, {path: '/c'}, a, b]}), [a, b])
  assert.deepEqual(Model.trustedRepositories({}), [])
  assert.deepEqual(Model.withTrust([a], b, true), [a, b])
  assert.deepEqual(Model.withTrust([a, b], a, false), [b])
  assert.deepEqual(Model.withTrust([a], {path: '/a', identity: '9:9:9'}, true), [{path: '/a', identity: '9:9:9'}])
  assert.ok(Model.isTrusted([a], {path: '/a', identity: '1:2:3'}))
  assert.ok(!Model.isTrusted([a], {path: '/a', identity: '1:2:4'}), 'a replaced repository is not trusted')
  assert.equal(Model.validTrustedList([a]), true)
  assert.match(Model.validTrustedList(['/a']), /absolute/)
  assert.match(Model.validTrustedList(Array(257).fill(a)), /256/)
  const rows = [{label:'b', usesLfs:true}, {label:'A', usesLfs:true}, {label:'c', usesLfs:false}]
  assert.deepEqual(Model.lfsRepos(rows).map(r => r.label), ['A', 'b'])
})

test('Settings lists only repositories using LFS; trust records of others stay', () => {
  const dotfiles = {label:'.dotfiles', path:'/home/me/.dotfiles', identity:'1:1:1', usesLfs:false}
  const assets = {label:'assets', path:'/home/me/assets', identity:'2:2:2', usesLfs:true}
  const site = {label:'site', path:'/home/me/site', identity:'3:3:3', usesLfs:false}
  assert.deepEqual(Model.lfsRepos([dotfiles, assets, site]).map(r => r.label), ['assets'])
  assert.deepEqual(Model.lfsRepos([dotfiles, site]), [], 'no LFS repository: the section hides')
  // A trusted repository that stopped using LFS isn't listed (even when the
  // backend reports it trusted), but its record stays.
  assert.deepEqual(Model.lfsRepos([dotfiles, assets, {...site, trusted:true}]).map(r => r.label), ['assets'])
  const trusted = [{path: site.path, identity: site.identity}, {path: '/gone', identity: '9:9:9'}]
  // Toggling one repository never drops another's record, listed, found or not.
  assert.deepEqual(Model.withTrust(trusted, assets, true).map(e => e.path), ['/home/me/site', '/gone', '/home/me/assets'])
  assert.deepEqual(Model.withTrust(trusted, assets, false), trusted)
  assert.deepEqual(Model.trustedRepositories({trustedRepositories: trusted}), trusted)
})

test('status rows carry optional usesLfs/trusted booleans', () => {
  const row = {path:'/repo', name:'repo', owner:'p', label:'repo', branch:'main', upstream:'',
    ahead:0, behind:0, dirtyCount:0, complete:true, affected:false, error:''}
  const parsed = Model.parseStatus(JSON.stringify({ok:true, repos:[row]}))
  assert.equal(parsed.repos[0].usesLfs, false)
  assert.equal(parsed.repos[0].trusted, false)
  assert.equal(Model.parseStatus(JSON.stringify({ok:true, repos:[{...row, usesLfs:'yes'}]})).ok, false)
})

test('trust edits compose over the newest queued save (untrust B then C)', () => {
  const A = {path:'/a', identity:'1:1:1'}, B = {path:'/b', identity:'2:2:2'}, C = {path:'/c', identity:'3:3:3'}
  const effective = {trustedRepositories: [A, B, C]}
  let waiting = []
  // First edit while another save is in flight: queued.
  const first = Model.withTrust(Model.trustBase(effective, waiting), B, false)
  waiting = [{key: 'inotifyEnabled', value: false}, {key: 'trustedRepositories', value: first}]
  const second = Model.withTrust(Model.trustBase(effective, waiting), C, false)
  assert.deepEqual(second, [A], 'B must stay untrusted')
  // The old composition (effective settings only) restored B:
  assert.deepEqual(Model.withTrust(Model.trustedRepositories(effective), C, false), [A, B])
})

test('fetch helper output is validated before reporting', () => {
  assert.equal(Model.fetchOutcome(1, '', 'error: cannot create cache lock').status, 'error')
  assert.equal(Model.fetchOutcome(1, '', 'error: cannot create cache lock').message, 'error: cannot create cache lock')
  assert.equal(Model.fetchOutcome(1, '{}', '').status, 'error')
  assert.equal(Model.fetchOutcome(1, 'garbage', '').message, 'Repository synchronization failed')
  assert.deepEqual(Model.fetchOutcome(1, JSON.stringify({ok: false, failures: [{}, {}]}), ''), {status: 'done', message: '', failed: 2})
  assert.equal(Model.fetchOutcome(0, JSON.stringify({ok: true, failures: []}), '').failed, 0)
  assert.equal(Model.fetchOutcome(0, JSON.stringify({busy: true, message: 'x'}), '').status, 'busy')
})
