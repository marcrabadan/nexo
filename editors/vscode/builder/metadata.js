const { createHash } = require('crypto');
const { validate } = require('./model');
const prefix = '# nexo-builder-model: ';
function strip(source) { return source.split(/\r?\n/).filter(line => !line.startsWith(prefix)).join('\n'); }
function digest(source) {
  // Runtime edits do not invalidate visual metadata; view/style edits do.
  const visual = []; let section = false;
  for (const line of source.split(/\r?\n/)) {
    if (/^(?:pub )?(component|page|layout) /.test(line)) visual.push(line);
    if (line.trim() && !/^ {8}/.test(line)) section = /^    (view|style):/.test(line);
    if (section) visual.push(line);
  }
  return createHash('sha256').update(visual.join('\n')).digest('hex');
}
function readMetadata(source) {
  const line = source.split(/\r?\n/).find(line => line.startsWith(prefix));
  if (!line) return;
  try {
    const value = JSON.parse(Buffer.from(line.slice(prefix.length), 'base64').toString('utf8'));
    if (value.hash === digest(strip(source))) return validate(value.model);
  } catch { /* A stale or invalid snapshot falls back to source parsing. */ }
}
function withMetadata(source, model) {
  const clean = strip(source);
  return prefix + Buffer.from(JSON.stringify({ hash: digest(clean), model: validate(model) })).toString('base64') + '\n' + clean;
}
module.exports = { readMetadata, withMetadata, strip };
