/* Minimal, dependency-free Markdown → HTML for streamed model output.
   Scoped deliberately to what the composer actually emits: paragraphs, headings, ordered
   and unordered lists (two levels), fenced code, tables, blockquotes, rules, and inline
   code/bold/italic/strike/links.

   Two Spacebot-specific affordances: a line starting "✓" becomes a verification callout,
   and one starting "Heads up:" becomes a warning callout — both are things the compose
   prompt asks the model for, so they earn dedicated styling.

   Everything is escaped before any markup is inserted, and code spans are lifted out
   before emphasis runs so `**` inside a command is never eaten. */

export function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// Private-use sentinels. They pass through escapeHtml untouched and never appear in real
// text, so a lifted code span comes back exactly where it was — spacing included.
const OPEN = '';
const CLOSE = '';
const SPAN = /(\d+)/g;

function inline(src) {
  const code = [];
  // Lift code spans first so emphasis/link rules can't touch their contents.
  let s = String(src ?? '').replace(/`([^`\n]+)`/g, (_, c) => {
    code.push(c);
    return OPEN + (code.length - 1) + CLOSE;
  });
  s = escapeHtml(s);
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, t, u) => `<a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">${t}</a>`);
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    (_, p, u) => `${p}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
  s = s.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  s = s.replace(/(^|[^_])__([^_]+)__/g, '$1<strong>$2</strong>');
  s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
  return s.replace(SPAN, (_, i) => `<code>${escapeHtml(code[+i])}</code>`);
}

/* Known verification strings for the answer being rendered, supplied by the server from
   the workflow itself. Models reproduce the words reliably but drop the ✓ marker about
   half the time, so we recognise the sentence rather than trusting the marker. */
let verifications = [];
const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

function isVerification(t) {
  if (!verifications.length) return false;
  const n = norm(t);
  if (n.length < 13) return false;
  // Near-equality, not containment. Containment marked any long line that happened to
  // quote the check — including the step body itself — turning a whole paragraph green.
  return verifications.some(v => n === v || (n.startsWith(v) && n.length <= v.length + 10));
}

/* A paragraph-ish line, upgraded to a callout when it opens with one of our markers. */
function callout(text) {
  const t = text.trim();
  // The ✓ / "!" glyphs are drawn by CSS, so strip any the model already wrote.
  if (/^✓/.test(t)) return `<div class="verify"><span>${inline(t.replace(/^✓\s*/, ''))}</span></div>`;
  const hu = t.match(/^(?:heads up|note|watch out|careful)\s*[:—-]\s*(.*)$/i);
  if (hu) return `<div class="heads-up"><span>${inline(hu[1])}</span></div>`;
  if (isVerification(t)) return `<div class="verify"><span>${inline(t)}</span></div>`;
  return null;
}

const RE = {
  fence: /^\s*```(\w*)\s*$/,
  ol: /^(\s*)(\d+)[.)]\s+(.*)$/,
  ul: /^(\s*)[-*+]\s+(.*)$/,
  h: /^(#{1,6})\s+(.*)$/,
  hr: /^\s*([-*_])(\s*\1){2,}\s*$/,
  quote: /^\s*>\s?(.*)$/,
  trow: /^\s*\|(.+)\|\s*$/,
  tsep: /^\s*\|?[\s:-]*-[-\s|:]*\|?\s*$/,
};

function cells(row) {
  return row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
}

/* Render the body of one list item: first line inline, continuation lines as callouts
   or nested lists. Keeps `✓ You should see …` attached to the step it belongs to. */
function itemHtml(parts) {
  const [head, ...rest] = parts;
  let html = inline(head);
  const tail = rest.filter(x => x.trim() !== '');
  if (tail.length) {
    const nested = tail.filter(x => RE.ul.test(x) || RE.ol.test(x));
    const plain = tail.filter(x => !RE.ul.test(x) && !RE.ol.test(x));
    for (const p of plain) html += callout(p) || `<p>${inline(p.trim())}</p>`;
    if (nested.length) html += render(nested.map(x => x.trim()).join('\n'));
  }
  return html;
}

/* Call once per answer, before rendering it. */
export function setVerifications(list) {
  verifications = (list || []).map(norm).filter(v => v.length > 12);
}

export function render(src) {
  const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const ln = lines[i];

    // fenced code (an unterminated fence still renders — it happens mid-stream)
    const f = ln.match(RE.fence);
    if (f) {
      const lang = f[1] || '';
      const buf = [];
      i++;
      while (i < lines.length && !RE.fence.test(lines[i])) buf.push(lines[i++]);
      if (i < lines.length) i++;
      out.push(
        `<pre><div class="codehead"><span>${escapeHtml(lang || 'code')}</span>` +
        `<button class="copycode" type="button">Copy</button></div>` +
        `<div class="codebody"><code>${escapeHtml(buf.join('\n'))}</code></div></pre>`);
      continue;
    }

    if (RE.hr.test(ln)) { out.push('<hr>'); i++; continue; }

    const h = ln.match(RE.h);
    if (h) { out.push(`<h3>${inline(h[2])}</h3>`); i++; continue; }

    // table: a pipe row followed by a separator row
    if (RE.trow.test(ln) && i + 1 < lines.length && RE.tsep.test(lines[i + 1])) {
      const head = cells(ln);
      i += 2;
      const body = [];
      while (i < lines.length && RE.trow.test(lines[i])) body.push(cells(lines[i++]));
      out.push(
        '<div class="tablewrap"><table><thead><tr>' +
        head.map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>' +
        body.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') +
        '</tbody></table></div>');
      continue;
    }

    if (RE.quote.test(ln)) {
      const buf = [];
      while (i < lines.length && RE.quote.test(lines[i])) buf.push(lines[i++].match(RE.quote)[1]);
      out.push(`<blockquote>${render(buf.join('\n'))}</blockquote>`);
      continue;
    }

    // lists — collect items plus their indented continuation lines
    const isOl = RE.ol.test(ln);
    if (isOl || RE.ul.test(ln)) {
      const re = isOl ? RE.ol : RE.ul;
      const items = [];
      const baseIndent = ln.match(re)[1].length;
      while (i < lines.length) {
        const m = lines[i].match(re);
        if (m && m[1].length <= baseIndent + 1) {
          items.push([isOl ? m[3] : m[2]]);
          i++;
          // continuation: indented, non-blank, and not the start of a sibling item
          while (i < lines.length && lines[i].trim() !== '' && /^\s+/.test(lines[i]) &&
                 !re.test(lines[i])) {
            items[items.length - 1].push(lines[i++]);
          }
          continue;
        }
        // one blank line between items keeps the list going; anything else ends it
        if (lines[i].trim() === '' && i + 1 < lines.length && re.test(lines[i + 1])) { i++; continue; }
        break;
      }
      const tag = isOl ? 'ol' : 'ul';
      out.push(`<${tag}>` + items.map(p => `<li>${itemHtml(p)}</li>`).join('') + `</${tag}>`);
      continue;
    }

    if (ln.trim() === '') { i++; continue; }

    // paragraph: soft-wrapped lines until a blank line or the start of another block
    const para = [ln];
    i++;
    while (i < lines.length && lines[i].trim() !== '' &&
           !RE.ol.test(lines[i]) && !RE.ul.test(lines[i]) && !RE.h.test(lines[i]) &&
           !RE.fence.test(lines[i]) && !RE.hr.test(lines[i]) && !RE.quote.test(lines[i]) &&
           !RE.trow.test(lines[i])) {
      para.push(lines[i++]);
    }
    // callout markers apply per-line, so split them out of the paragraph
    const buf = [];
    const flush = () => {
      if (buf.length) { out.push(`<p>${inline(buf.join(' '))}</p>`); buf.length = 0; }
    };
    for (const p of para) {
      const c = callout(p);
      if (c) { flush(); out.push(c); } else { buf.push(p); }
    }
    flush();
  }

  return out.join('');
}
