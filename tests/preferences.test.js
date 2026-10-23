const I18n = require('./i18n.cjs');
const assert = require('node:assert/strict')
const test = require('node:test')
const Model = require('../Model.js')
const manifest = require('../manifest.json')
const nb = (label, values) => I18n.text(label, 'nb', values)

test('labels translate without changing unknown diagnostics or substituted repository names', () => {
  assert.equal(nb('Settings'), 'Innstillinger')
  assert.equal(I18n.text('Language', 'en'), 'Language')
  assert.equal(nb('fatal: origin is unavailable'), 'fatal: origin is unavailable')
  assert.equal(nb('constructor'), 'constructor')
  assert.equal(nb('Push %1', ['my-%1-$&-repo']), 'Send my-%1-$&-repo')
  assert.equal(nb('%1 level', [1]), '1 nivå')
  assert.equal(nb('%1 levels', [5]), '5 nivåer')
  assert.match(nb(Model.parseFolderRows([{path:'relative', depth:2}]).error), /absolutt sti/)
})

test('repository metadata and timestamps use the selected language and keep English defaults', () => {
  const repo = {branch:'feature/%1', ahead:1, behind:2, dirtyCount:3}
  assert.equal(Model.repositoryMeta(repo), 'feature/%1 · ↑1 commit · ↓2 commits · 3 changed')
  assert.equal(Model.repositoryMeta(repo, nb), 'feature/%1 · ↑1 innsending · ↓2 innsendinger · 3 endret')
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
  assert.equal(Model.updatedText(940, 1000000), 'Updated 1 minute ago')
  assert.equal(Model.updatedText(940, 1000000, nb), 'Oppdatert for 1 minutt siden')
  assert.equal(Model.updatedText(880, 1000000), 'Updated 2 minutes ago')
  assert.equal(Model.updatedText(880, 1000000, nb), 'Oppdatert for 2 minutter siden')
})
