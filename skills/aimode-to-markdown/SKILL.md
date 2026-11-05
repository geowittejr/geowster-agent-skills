---
name: aimode-to-markdown
description: Export a Google AI Mode shared conversation (a share.google/aimode/... link) to a Markdown transcript file. Use when the user gives an AI Mode share link and asks to save, export, read, archive or convert the chat/transcript to Markdown.
---

# Google AI Mode share link → Markdown transcript

Turns a Google AI Mode shared conversation into a clean `.md` file: every question with its timestamp, every answer with headings, lists, tables, code and bold kept, and the source cards listed under each answer.

A shared AI Mode page is rendered by JavaScript. `curl` and WebFetch don't get the conversation, so you need a real browser. The work is split in two:

| File | Runs in | Job |
|---|---|---|
| `scripts/extract_aimode.js` | the browser page (JavaScript tool) | Finds each turn, cleans the answer HTML, collects source cards and saves them as JSON |
| `scripts/aimode_to_markdown.py` | the shell (Python 3.8+, standard library only) | Converts that JSON to Markdown |

Paths below are relative to this skill's directory.

## Requirements

- A browser tool that can run JavaScript in a page. That is **Claude in Chrome** (`mcp__claude-in-chrome__*`) or the desktop app's built-in browser (`mcp__Claude_Browser__*`). The steps are the same for either; the tool names differ.
- `python3` on PATH.

## Steps

### 1. Open the link

1. Call `tabs_context_mcp` (with `createIfEmpty: true`), then `tabs_create_mcp` for a fresh tab.
2. `navigate` to the share URL. It redirects to `google.com/search?...udm=50`. Run `navigate` **on its own**, not inside `browser_batch`: a batch can't act on a page it just navigated to.
3. Take a small screenshot (`scale: 0.5`) to confirm the conversation shows, not a consent or sign-in page. If a consent banner appears, choose the most privacy-preserving option.

### 2. Run the extractor

Read `scripts/extract_aimode.js` and pass **its whole contents** as the `text` of `javascript_tool` (`action: "javascript_exec"`). The script uses top-level `await`, which the tool supports, so it returns a summary like:

```json
{ "turns": 5, "questions": ["…"], "sourcesPerTurn": [8,0,4,0,5],
  "jsonChars": 62474, "b64Length": 17988, "chunks": 20, "downloaded": "aimode-<slug>.json" }
```

By default the script **downloads** `aimode-<slug>.json` to the browser's Downloads folder. That is the fast path when the browser and Claude Code run on the same machine. To skip the download, put this as the first line of the same call:

```js
window.__AIMODE_OPTS = { download: false };
```

Before converting, check that `turns` matches the number of questions visible on the page. If it says `error`, the page hasn't finished loading or isn't an AI Mode page.

### 3a. Get the JSON from the download (preferred)

```bash
ls -t ~/Downloads/aimode-*.json | head -1
```

Use the newest file the extractor just created. Downloads can land elsewhere (e.g. `/mnt/c/Users/<you>/Downloads` under WSL), so check the browser's download location if it isn't there.

### 3b. Fallback: read it back in chunks

Use this when the download can't be reached, e.g. the browser runs on another machine. The browser tool cuts every result at about **1,000 characters**, so the script also keeps a gzip + base64url copy in `window.__aimode.b64`. Use the `chunks` count from the summary:

1. Fetch the chunks with `browser_batch`, about 10 `javascript_tool` calls per batch, each `window.__aimode.b64.slice(i*900, (i+1)*900)`.
2. Write the chunks **in order**, one per line, to a file such as `aimode.b64`. Line breaks between chunks are ignored.
3. Copy each chunk exactly. gzip's checksum makes the converter fail loudly if any character is wrong; if it does, fetch that range again.

### 4. Convert to Markdown

```bash
python3 scripts/aimode_to_markdown.py <aimode-*.json | aimode.b64> \
  -o <output>.md --share-url "<the original share.google link>"
```

Options:
- `--no-emoji`: label speakers "You" / "AI Mode" instead of 🧑 / 🤖.
- `--no-heading-heuristic`: don't turn short "1. Title" or bold-only paragraphs into `###` headings.
- `-o` defaults to `aimode-<title-slug>.md` in the current directory.

### 5. Verify, then clean up

- Open the `.md` and compare it with the page. The number of `## 🧑 You` headings should equal the number of turns, and spot-check one answer and one table.
- Close the tab you opened (`tabs_close_mcp`).
- Tell the user where the file is, and mention any blanks noted below.

## Output format

```markdown
# AI Mode Conversation: <title>
*Source: … — <share link>*  /  *Captured: <date>*
> Note about removed citation chips
---
## 🧑 You — 3:41 PM
**<question>**
## 🤖 AI Mode
<answer: ### headings, lists, tables, ``` code, **bold**>
**Sources:**
- Site · Author — [Title](url) — snippet
```

## How it works (for maintenance)

- **Finding turns.** Google adds hidden headings before each turn: `<h2>You said: …</h2>` and `<h3>AI Mode reply for …</h3>`. The extractor walks text nodes for those prefixes and takes each heading's parent as the turn container. These headings are for screen readers, so they are the most stable hook on the page.
- **Answer body.** The answer is in `.mZJni`; if that class is missing, the whole reply container is used. The extractor clones it and then:
  - removes elements hidden on screen (a feedback dialog, legal links, a textarea live in there), by checking computed styles on the live page and mapping each element to its clone by document order;
  - removes citation chips (`.DHPVt`), buttons, icons and form controls;
  - unwraps `<span>`s and strips attributes except `role`, `aria-level` and `colspan`/`rowspan`.
- **Never drop `<mark>`.** AI Mode wraps its highlighted key answer in `<mark>` ("on **April 13, 2025**", "about **1 to 2 hours**…"). An earlier version removed `<mark>` and lost exactly the most important facts.
- **Headings.** Section titles are `role="heading" aria-level="N"` divs, not `<h*>` tags. The converter shifts levels so each answer's top level renders as `###`.
- **Source cards** sit outside the answer body. Each is an empty `<a aria-label="Title">` whose parent div holds the site, title and snippet. Cards collapsed behind "Show all" give their text as one run-together line, so the extractor reads each element's own text nodes instead. ", Video, watch on YouTube" is removed from titles, and `google.com/url?q=` redirects are unwrapped.
- **Split bold.** Some list items render as `<a>Git</a><strong>:</strong>`. The converter rewrites the resulting `Git**:**` to `**Git:**`.
- **Why the extractor returns only a summary.** The browser tool truncates results at about 1,000 characters, and Claude in Chrome blocks results that look like URLs with query strings. Returning raw HTML or links fails, so data leaves through the download or the base64 chunks.

### If Google changes the page

Obfuscated class names (`.mZJni`, `.DHPVt`) are the likeliest thing to break. To find the new ones, run a small outline script on one reply container that prints `tag.firstClass "first 40 chars of own text"` for each element. Look for:
- the div that wraps all the answer paragraphs → new `.mZJni`;
- the span that holds a `<button>` after each sentence → new `.DHPVt`.

Update the two constants at the top of section 2 of `extract_aimode.js`.

## Known limitations

- **Blanks in Google's page.** AI Mode sometimes leaves a word out of a shared page, e.g. "Yes, ___ essentially strips away…" or "outputs ___." These blanks are empty in the page itself, so the export has them too. If you notice one, add `[blank in original]` by hand and tell the user.
- Images, charts and embedded videos in answers are dropped; only their text is kept.
- Very long conversations: make sure all turns have loaded (scroll to the top if earlier turns are lazy-loaded) before running the extractor.
