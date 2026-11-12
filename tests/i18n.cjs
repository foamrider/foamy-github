// The kit's FoamyI18n text code (kit/FoamyI18nText.js, which the panel's
// FoamyI18n substitutes through) over this plugin's catalogs, for pure model tests.
const I18nText = require('../kit/node-loader.cjs').load('FoamyI18nText');
const en = require('../i18n/en.json');
const no = require('../i18n/no.json');
const translators = {no: I18nText.translator(no, en), en: I18nText.translator(en, en)};
function translator(language) { return language === 'no' || language === 'nb' ? translators.no : translators.en; }
function text(key, language, values) { return translator(language).t(key, values); }
function plural(key, count, language, values) { return translator(language).tn(key, count, values); }
module.exports = {text, plural, no, en, norwegian: no, nb: no};
