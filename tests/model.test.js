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
  assert.equal(Model.repositoryState(after.repos[0]), 'unavailable')
  assert.match(Model.barText(after, false), /1\?/)
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
