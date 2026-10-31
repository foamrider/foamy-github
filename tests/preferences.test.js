const I18n = require('./i18n.cjs');
const assert = require('node:assert/strict')
const test = require('node:test')
const Model = require('../Model.js')
const manifest = require('../manifest.json')
const nb = (label, values) => I18n.text(label, 'nb', values)
const nbn = (label, count) => I18n.plural(label, count, 'nb')
const enn = (label, count) => I18n.plural(label, count, 'en')

test('labels translate without changing unknown diagnostics or substituted repository names', () => {
  assert.equal(nb('Settings'), 'Innstillinger')
  assert.equal(I18n.text('Language', 'en'), 'Language')
  assert.equal(nb('fatal: origin is unavailable'), 'fatal: origin is unavailable')
  assert.equal(nb('constructor'), 'constructor')
  assert.equal(nb('Push %1', ['my-%1-$&-repo']), 'Send my-%1-$&-repo')
  assert.equal(nbn('%1 levels', 1), '1 nivå')
  assert.equal(nbn('%1 levels', 5), '5 nivåer')
  assert.equal(enn('%1 repositories', 1), '1 repository')
  assert.match(nb(Model.parseFolderRows([{path:'relative', depth:2}]).error), /absolutt sti/)
})

test('repository metadata and timestamps use the selected language and keep English defaults', () => {
  const repo = {branch:'feature/%1', ahead:1, behind:2, dirtyCount:3}
  assert.equal(Model.repositoryMeta(repo, undefined, enn), 'feature/%1 · ↑1 commit · ↓2 commits · 3 changed')
  assert.equal(Model.repositoryMeta(repo, nb, nbn), 'feature/%1 · ↑1 innsending · ↓2 innsendinger · 3 endret')
  assert.equal(Model.relativeTime(0, 1000000, nb), 'Aldri')
  assert.equal(Model.relativeTime(1000, 1000000, nb), 'Akkurat nå')
  assert.equal(Model.relativeTime(940, 1000000, nb), 'for 1 min siden')
  assert.equal(Model.relativeTime(940, 1000000), '1m ago')
  assert.equal(Model.relativeTime(1, 7201000, nb), 'for 2 t siden')
  assert.equal(Model.relativeTime(1, 172801000, nb), 'for 2 d siden')
})

test('the update label matches the other plugins and ages after the last successful scan', () => {
  for (const timestamp of [0, -1, NaN, undefined])
    assert.equal(Model.updatedText(timestamp, 1000000, nb), '')
  assert.equal(Model.updatedText(1000, 1000000), 'Updated just now')
  assert.equal(Model.updatedText(1000, 1000000, nb), 'Oppdatert nå')
  assert.equal(Model.updatedText(940, 1000000, undefined, enn), 'Updated 1 minute ago')
  assert.equal(Model.updatedText(940, 1000000, nb, nbn), 'Oppdatert for 1 minutt siden')
  assert.equal(Model.updatedText(880, 1000000), 'Updated 2 minutes ago')
  assert.equal(Model.updatedText(880, 1000000, nb, nbn), 'Oppdatert for 2 minutter siden')
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
  for (const [key, value] of Object.entries(I18n.en)) {
    if (typeof value === 'object') assert.deepEqual(Object.keys(value).sort(), ['one', 'other'], key)
    assert.ok(Object.hasOwn(I18n.no, key), `missing Norwegian: ${key}`)
  }
})
