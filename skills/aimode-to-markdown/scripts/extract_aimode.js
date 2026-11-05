// extract_aimode.js
// Run inside a loaded Google AI Mode conversation page (the page a
// share.google/aimode/... link redirects to) via the browser's JavaScript tool.
// Pass the WHOLE file as the script text. It uses top-level `await`, which the
// Claude in Chrome / built-in browser JavaScript tools support.
//
// What it does:
//   1. Finds every turn using Google's visually-hidden headings
//      ("You said: ..." and "AI Mode reply for ...").
//   2. For each reply, clones the visible answer body, drops hidden UI
//      (feedback dialogs, legal links), citation chips, buttons and icons,
//      unwraps <span>s and strips attributes, keeping simplified HTML for the
//      Python converter.
//   3. Collects the source cards shown under each reply (site, title, snippet, URL).
//   4. Stores the result on window.__aimode and, by default, downloads it as
//      aimode-<slug>.json. Also keeps a gzip+base64url copy for chunked
//      retrieval when the download can't be read back.
//
// Options (set BEFORE running, e.g. as the first line of the same call):
//   window.__AIMODE_OPTS = { download: false, chunkSize: 900 };
//
// Returns a small summary object only. It never returns raw HTML or URLs: the
// browser tool truncates results at ~1,000 characters and blocks output that
// looks like URL query strings.
await (async () => {
  const OPTS = Object.assign({ download: true, chunkSize: 900 }, window.__AIMODE_OPTS || {});

  // --- 1. Locate turns -------------------------------------------------------
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const users = [];
  const replies = [];
  let node;
  while ((node = walker.nextNode())) {
    const t = node.textContent.trim();
    if (/^AI Mode reply for/.test(t)) replies.push(node.parentElement.parentElement);
    else if (/^You said:/.test(t)) users.push(node.parentElement.parentElement);
  }
  if (!replies.length) {
    return { error: 'No AI Mode turns found. Make sure the conversation has fully loaded and this is an AI Mode share link.' };
  }

  // --- 2. Sanitize reply bodies ---------------------------------------------
  // Google's obfuscated class names observed Nov 2026 (fallbacks exist if they change):
  //   .mZJni = answer body container      .DHPVt = inline citation chip ("GitHub +2")
  // NOTE: never drop <mark> — AI Mode wraps its highlighted key answer in it.
  const DROP = 'script,style,noscript,template,svg,img,button,textarea,input,select,aside,.DHPVt';
  const KEEP_ATTRS = new Set(['role', 'aria-level', 'colspan', 'rowspan']);

  function isHidden(el) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return true;
    // Visually-hidden helpers (1px clip boxes) used for screen-reader-only labels
    if (cs.position === 'absolute' && el.offsetWidth <= 1 && el.offsetHeight <= 1) return true;
    return false;
  }

  function sanitize(el) {
    const live = [el, ...el.querySelectorAll('*')];
    const clone = el.cloneNode(true);
    const cloned = [clone, ...clone.querySelectorAll('*')];
    // Same document order in both lists, so index i maps live -> clone.
    const hidden = [];
    for (let i = 1; i < live.length; i++) if (isHidden(live[i])) hidden.push(cloned[i]);
    hidden.forEach((x) => x.remove());
    clone.querySelectorAll(DROP).forEach((x) => x.remove());
    clone.querySelectorAll('h1,h2,h3,h4').forEach((h) => {
      if (/^(AI Mode reply for|You said:)/.test(h.textContent.trim())) h.remove();
    });
    // Links with no text (citation anchors) add nothing.
    clone.querySelectorAll('a').forEach((a) => { if (!a.textContent.trim()) a.remove(); });
    // Unwrap spans (pure styling wrappers) to shrink the payload.
    clone.querySelectorAll('span').forEach((s) => s.replaceWith(...s.childNodes));
    for (const e of [clone, ...clone.querySelectorAll('*')]) {
      for (const a of [...e.attributes]) if (!KEEP_ATTRS.has(a.name)) e.removeAttribute(a.name);
    }
    // Drop empty divs left behind.
    let changed = true;
    while (changed) {
      changed = false;
      clone.querySelectorAll('div').forEach((d) => {
        if (!d.textContent.trim() && !d.querySelector('br,hr')) { d.remove(); changed = true; }
      });
    }
    return clone.innerHTML.replace(/\s{2,}/g, ' ');
  }

  // --- 3. Source cards -------------------------------------------------------
  // Each card is an <a aria-label="Title"> with no text whose parent div holds
  // "Site | Title | Snippet". Cards hidden behind "Show all" are included.
  function sources(replyEl, bodyEl) {
    const seen = new Set();
    const out = [];
    for (const a of replyEl.querySelectorAll('a[href]')) {
      if (bodyEl !== replyEl && bodyEl.contains(a)) continue; // anchors inside the answer
      const href = a.href || '';
      if (!/^https?:/.test(href) || /google\.[a-z.]+\/search/.test(href) || seen.has(href)) continue;
      const card = a.parentElement || a;
      let raw = (card.innerText || '').trim().split('\n');
      if (raw.filter((l) => l.trim()).length <= 1) {
        // Card is collapsed behind "Show all": innerText comes back as one
        // run-together line (or empty). Read each element's own text nodes
        // instead, which keeps site / title / snippet apart.
        raw = [...card.querySelectorAll('*')]
          .filter((e) => !e.closest('button'))
          .map((e) => [...e.childNodes].filter((c) => c.nodeType === 3).map((c) => c.textContent).join(''))
          .filter((t) => t.trim());
      }
      const lines = raw.map((l) => l.trim())
        .filter((l) => l && l !== '·' && !/^\d{1,2}:\d{2}(:\d{2})?$/.test(l));
      const title = (a.getAttribute('aria-label') || '').trim();
      if (!title && !lines.length) continue;
      seen.add(href);
      out.push({ href, title, lines });
    }
    return out;
  }

  // --- 4. Build turns --------------------------------------------------------
  const count = Math.min(users.length, replies.length);
  const turns = [];
  for (let i = 0; i < count; i++) {
    const u = users[i];
    const r = replies[i];
    const h2 = u.querySelector('h2');
    const question = (h2 ? h2.textContent : u.innerText.split('\n')[0]).replace(/^You said:\s*/, '').trim();
    const time = ((u.innerText || '').match(/\b\d{1,2}:\d{2}\s?[AP]M\b/i) || [''])[0];
    const body = r.querySelector('.mZJni') || r;
    turns.push({ question, time, reply_html: sanitize(body), sources: sources(r, body) });
  }

  const title = document.title.replace(/\s*-\s*Google Search\s*$/, '').trim();
  const data = {
    title,
    page_url: location.origin + location.pathname, // query string omitted on purpose
    captured: new Date().toISOString(),
    turn_count_mismatch: users.length !== replies.length ? { users: users.length, replies: replies.length } : null,
    turns,
  };
  const json = JSON.stringify(data);

  // --- 5. gzip + base64url copy for chunked retrieval ------------------------
  let b64 = '';
  try {
    const gz = await new Response(new Blob([json]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer();
    const bytes = new Uint8Array(gz);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    b64 = btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  } catch (e) {
    b64 = '';
  }
  const slug = (title || 'conversation').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60);
  window.__aimode = { json, b64, chunkSize: OPTS.chunkSize, filename: `aimode-${slug}.json` };

  // --- 6. Optional download (lands in the browser's Downloads folder) --------
  let downloaded = null;
  if (OPTS.download) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
    a.download = window.__aimode.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    downloaded = window.__aimode.filename;
  }

  return {
    turns: turns.length,
    questions: turns.map((t) => t.question.slice(0, 60)),
    sourcesPerTurn: turns.map((t) => t.sources.length),
    jsonChars: json.length,
    b64Length: b64.length,
    chunks: b64 ? Math.ceil(b64.length / OPTS.chunkSize) : 0,
    chunkSize: OPTS.chunkSize,
    downloaded,
  };
})();
