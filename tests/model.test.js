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
