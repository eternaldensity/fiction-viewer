# The Cosy Nook — static fiction viewer

A neat, tidy, cosy place to read stories. Served as plain static files, so it
runs anywhere (GitHub Pages, Netlify, a USB stick).

## How it works

```
stories/<series>/<story>.odt   <- you edit these
assets/style.css, assets/app.js <- site chrome (edit freely)
tools/build.py                  <- generator (stdlib only, no installs)
site/                           <- generated output (deployed to Pages)
```

- **Series are folders.** Every immediate subdirectory of `stories/` becomes a
  series shelf. `stories/uni-hack/` is the current one.
- **Stories are documents.** Every `*.odt` in a series folder becomes a story,
  ordered by its filename prefix (`uh1 Unlocking.odt`, `uh2 unmasking.odt`).
- **Chapters are headings.** Lines reading `Function N` (optionally preceded by
  `Uni-Hack`) or `Chapter N`, with an optional short subtitle line underneath,
  each become a chapter page. Everything before the first one becomes
  *Opening*. Lines of only `*` or `=` become quiet scene-break ornaments.
- **Formatting is preserved.** Source colours (the blue headings, red
  disclaimer, grey asides), relative sizes (9/11/12/14pt), bold/italic,
  centring and indents are carried into the page with theme-aware CSS. Font
  families are normalised to the reading font on purpose, so 30k-word stories
  stay easy on the eyes. Dark / sepia themes remap the preserved colours so
  they stay readable.

## Preview locally

```sh
python3 tools/build.py
python3 -m http.server -d site 8000
# open http://localhost:8000
```

No dependencies. Python 3.9+ is enough.

## Publish with GitHub Pages

1. Push this repo to GitHub on branch `main`.
2. In **Settings → Pages**, set **Source** to **GitHub Actions**.
3. Push an edit (or use *Actions → Build and deploy fiction site → Run
   workflow*). The workflow runs `python3 tools/build.py` and deploys `site/`.

To update the library, just add or edit an `.odt` under `stories/` and push —
new series, stories and chapters appear automatically, including the home page,
series pages, story pages, chapter prev/next chains and `search.json`.

## Tips for authors

- Keep chapter titles on their own line: `Function 12` / `Chapter 4`.
- Put the chapter's subtitle on the very next non-blank line
  (e.g. `The First Quackers`, `Found and Hidden`).
- Scene breaks: a line of `******` or `==========================` that does
  *not* sit above a chapter title renders as a small ornament, not a chapter.
- The opening matter (disclaimer, title, epigraph) before the first chapter
  title is kept as the *Opening* chapter.
- Filenames control order and display title: `uh1 Unlocking.odt` → story 1,
  *Unlocking*. All-lowercase titles are capitalised; anything else is kept
  verbatim.
