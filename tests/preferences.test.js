const I18n = require('./i18n.cjs');
const assert = require('node:assert/strict')
const test = require('node:test')
const Model = require('../Model.js')
const manifest = require('../manifest.json')
const Schema = require('../Preferences.js')
const Prefs = require('../kit/node-loader.cjs').load('FoamyPrefs')
const fields = Schema.fields.concat(Model.listFields)
// The kit runs in its own vm context: copy its values into this realm.
const plain = value => value === undefined ? value : JSON.parse(JSON.stringify(value))
const nb = (label, values) => I18n.text(label, 'nb', values)
const nbn = (label, count) => I18n.plural(label, count, 'nb')
const enn = (label, count) => I18n.plural(label, count, 'en')

test('Model\'s messages to the panel have catalog entries; diagnostics pass through', () => {
  // Git's own diagnostics are not catalog keys and show as they are.
  assert.equal(nb('fatal: origin is unavailable'), 'fatal: origin is unavailable')
  // A key that names an Object member is not taken from the prototype.
  assert.equal(nb('constructor'), 'constructor')
  const messages = [
    Model.parseFolderRows([{path:'relative', depth:2}]).error,
    Model.parseFolderRows([{path:'/a', depth:9}]).error,
    Model.parseFolderRows(Array(33).fill('/a')).error,
    Model.validTrustedList(['/a']),
    Model.validTrustedList(Array(257).fill({path:'/a', identity:'1'})),
    Model.fetchOutcome(1, '', '').message,
    Model.parseStatus('').error,
    Model.parseStatus('x').error,
    Model.parseStatus('{"ok":true,"repos":[{}]}').error,
    Model.parseStatus('[]').error,
  ]
  for (const message of messages) {
    assert.equal(typeof message, 'string', String(message))
    assert.ok(Object.hasOwn(I18n.en, message), `no English entry: ${message}`)
    assert.ok(Object.hasOwn(I18n.no, message) && I18n.no[message] !== '', `no Norwegian entry: ${message}`)
  }
})

test('repository metadata and timestamps use the selected language and keep English defaults', () => {
  const repo = {branch:'feature/%1', ahead:1, behind:2, dirtyCount:3}
  // A branch name is never substituted into.
  assert.equal(Model.repositoryMeta(repo, undefined, enn), 'feature/%1 · ↑1 commit · ↓2 commits · 3 changed')
  assert.equal(Model.repositoryMeta(repo, nb, nbn),
    `feature/%1 · ↑${nbn('%1 commits', 1)} · ↓${nbn('%1 commits', 2)} · ${nb('%1 changed', [3])}`)
  const now = 1000000000
  const ago = seconds => Model.relativeTime(now / 1000 - seconds, now)
  assert.equal(Model.relativeTime(0, now), 'Never')
  assert.equal(ago(44), 'Just now')
  assert.notEqual(ago(45), 'Just now')
  assert.equal(ago(60), '1m ago')
  assert.equal(ago(59 * 60 + 59), '59m ago')
  assert.equal(ago(60 * 60), '1h ago')
  assert.equal(ago(24 * 3600 - 1), '23h ago')
  assert.equal(ago(24 * 3600), '1d ago')
  assert.equal(Model.relativeTime(now / 1000 - 60, now, nb), nb('%1m ago', [1]))
})

test('fetch errors render inline after the status line and translate', () => {
  const repo = {branch:'main', ahead:0, behind:0, dirtyCount:10, complete:true,
    syncError:'No origin remote configured. Add one in lazygit, then refresh.'}
  assert.equal(Model.repositoryMeta(repo, undefined, enn), 'main · 10 changed')
  assert.equal(Model.syncErrorText(repo), 'No origin remote configured. Add one in lazygit, then refresh.')
  assert.notEqual(Model.syncErrorText(repo, nb), repo.syncError)
  assert.equal(Model.syncErrorText({...repo, syncError:''}), '')
})

test('every catalog plural has one and other forms, and Norwegian covers every English key', () => {
  const tokens = text => [...text.matchAll(/%[1-9][0-9]*|\{\w+\}/g)].map(m => m[0]).sort()
  const forms = value => typeof value === 'object' ? Object.values(value) : [value]
  for (const [key, value] of Object.entries(I18n.en)) {
    if (typeof value === 'object') assert.deepEqual(Object.keys(value).sort(), ['one', 'other'], key)
    else assert.equal(value, key, `English source key: ${key}`)
    assert.ok(Object.hasOwn(I18n.no, key), `missing Norwegian: ${key}`)
    // The kit shows English for an empty translation: an empty entry is a missing one.
    for (const form of forms(I18n.no[key])) {
      assert.notEqual(form, '', `empty Norwegian: ${key}`)
      assert.deepEqual(tokens(form), tokens(forms(value).at(-1)), `placeholders of ${key}`)
    }
  }
})

test('every string the panel passes to FoamyI18n has an entry', () => {
  const fs = require('node:fs')
  const path = require('node:path')
  const root = path.join(__dirname, '..')
  const literal = '"((?:[^"\\\\]|\\\\.)*)"'
  const files = fs.readdirSync(root).filter(name => name.endsWith('.qml')).concat(['Model.js'])
  const keys = new Set()
  for (const name of files) {
    const source = fs.readFileSync(path.join(root, name), 'utf8')
    const patterns = [new RegExp('FoamyI18n\\.tn?\\(' + literal, 'g')]
    // The Service hands these to FoamyI18n later, through actionStatus.
    if (name === 'Service.qml') patterns.push(new RegExp('setActionStatus\\(' + literal, 'g'), new RegExp('label: ' + literal, 'g'))
    for (const pattern of patterns)
      for (const match of source.matchAll(pattern)) if (match[1] !== '') keys.add(JSON.parse(`"${match[1]}"`))
  }
  // A floor, so that a pattern which stops matching fails here.
  assert.ok(keys.size >= 60, `only ${keys.size} keys found`)
  for (const key of keys) {
    assert.ok(Object.hasOwn(I18n.en, key), `no English entry: ${key}`)
    assert.ok(Object.hasOwn(I18n.no, key), `no Norwegian entry: ${key}`)
  }
})

test('the schema mirrors the manifest schema and defaults', () => {
  for (const entry of manifest.barWidget.schema) {
    const field = fields.find(f => f.key === entry.key)
    assert.ok(field, entry.key)
    for (const prop of ['type', 'min', 'max', 'step', 'defaultValue']) assert.equal(field[prop], entry[prop], `${entry.key}.${prop}`)
  }
  for (const [key, value] of Object.entries(manifest.barWidget.defaults))
    assert.deepEqual(plain(Prefs.value(fields, {}, key)), value, key)
})

test('the service reads settings through the schema: an invalid value is the default, 0 turns refresh off', () => {
  const read = settings => plain(Prefs.values(fields, settings))
  assert.deepEqual(read({}), {inotifyEnabled:true, refreshIntervalSec:30, fetchIntervalSec:900,
    trustRepositoryConfig:false, trustedRepositories:[], repositoryFolders:[]})
  assert.equal(read({refreshIntervalSec:0}).refreshIntervalSec, 0)
  assert.equal(read({refreshIntervalSec:5}).refreshIntervalSec, 5)
  assert.equal(read({refreshIntervalSec:3600}).refreshIntervalSec, 3600)
  // Out of range or the wrong type reads as the default, never a clamp.
  for (const value of [-1, 3601, 30.5, '60', null, true]) assert.equal(read({refreshIntervalSec:value}).refreshIntervalSec, 30, String(value))
  for (const value of [0, 299, 86401, '900']) assert.equal(read({fetchIntervalSec:value}).fetchIntervalSec, 900, String(value))
  assert.equal(read({inotifyEnabled:false}).inotifyEnabled, false)
  assert.equal(read({inotifyEnabled:'false'}).inotifyEnabled, true)
  assert.equal(read({trustRepositoryConfig:true}).trustRepositoryConfig, true)
  assert.equal(read({trustRepositoryConfig:'true'}).trustRepositoryConfig, false)
})

test('repository folders and trusted repositories reach the helpers only when valid', () => {
  const read = (key, value) => plain(Prefs.value(fields, {[key]: value}, key))
  const folders = [{path:'~/GitHub', depth:3}, '/srv/legacy']
  assert.deepEqual(read('repositoryFolders', folders), folders)
  for (const value of [[{path:'relative', depth:2}], [{path:'/tmp', depth:6}], Array(33).fill('/tmp'), '~/GitHub', {}])
    assert.deepEqual(read('repositoryFolders', value), [], JSON.stringify(value))
  const trusted = [{path:'/r/a', identity:'1:1:1|2:2:2'}]
  assert.deepEqual(read('trustedRepositories', trusted), trusted)
  for (const value of [['/r/a'], [{path:'r/a', identity:'1:1:1'}], [{path:'/r/a', identity:''}], 'x'])
    assert.deepEqual(read('trustedRepositories', value), [], JSON.stringify(value))
})
