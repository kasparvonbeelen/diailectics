# diailectics

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/kasparvonbeelen/diailectics/blob/main/interface.ipynb)

Diailectics experiments with Claude

A persona-based Claude chat interface for the Gamson & Modigliani "Runaway"
frame-annotation exercise. Paste a newspaper article; Claude selects the
passages worth annotating against the coding schema, then four personas —
Devil's Advocate, Sycophant, Hegel, Adorno — each comment on every passage,
including their own yes/no stance on whether it carries the Runaway frame.

- **Comments sit beside the text.** The highlighted article is on the left, a
  column of per-passage comments on the right.
- **Each passage is labelled yes / no / maybe**, from how much the personas'
  stances actually overlap — "maybe" means they split, and the label says who
  landed where. The article's highlight color shows the same thing, so you can
  scan for where the personas disagree.
- **Replies keep their history.** Reply to any persona and it answers in
  character; the earlier version isn't overwritten, so the thread shows the
  whole exchange.
- **Add your own passages** for text the automatic selection skipped — they
  join the same comments column.
- **Save** everything to a timestamped JSON file: each passage, its
  annotation, and every persona's full response history plus your replies.

## Contents

- `tools/claude_chat.py` — the backend: parses the personas out of
  `persona_test_prompts.md`, flattens `runaway_package_annotator_instructions.html`
  into the coding schema, calls Claude, and renders styled HTML output. No
  ipywidgets dependency, so it's usable headlessly / in scripts.
- `tools/notebook_ui.py` — the ipywidgets front end over that backend;
  `build_interface(backend)` returns the whole UI as one widget.
- `interface.ipynb` — a thin notebook that wires the two together (it's mostly
  `ui = build_interface(backend)`); also runnable on Colab via the badge above.
- `persona_test_prompts.md` — the four persona instruction prompts.
- `runaway_package_annotator_instructions.html` — the Runaway-frame coding
  schema and worked examples (Gamson & Modigliani 1989).

## Setup

```bash
pip install -r requirements.txt
```

You need an Anthropic API key. `tools/claude_chat.py` resolves it in this
order:

1. `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) in your shell environment
2. A `.env` file in this folder (`ANTHROPIC_API_KEY=sk-ant-...`) — loaded via
   `python-dotenv`
3. An `api_key.json` file in this folder with an `"anthropic_api_key"` field
4. Whatever `anthropic.Anthropic()` finds on its own (e.g. `ant auth login`)

**None of these credential files are tracked by git** (see `.gitignore`) — set
one of them up locally before running the notebook.

On Colab, use the badge above instead — the notebook's first cells clone this
repo, install the requirements, and look for a key in the environment, in a
Colab secret named `ANTHROPIC_API_KEY`, or in an uploaded `api_key.json`.

## Running it

```bash
jupyter notebook interface.ipynb
```

Paste an article, pick a model and which personas to run, and click
"Annotate article". Expand a persona's tab to read its full response and
write a reply — it's sent back to that persona as a follow-up turn.

Each run makes `1 + (passages found × personas selected)` API calls,
concurrently — e.g. 4 passages × 4 personas = 17 calls — plus one per reply.
"Max passages" and the persona checkboxes control that.

See the module docstring in `tools/claude_chat.py` for the programmatic API
(`ChatBackend.run()` for a single passage, `ChatBackend.run_passage()` for one
passage across several personas, `ChatBackend.annotate_article()` for a whole
article), and `tools/notebook_ui.py` for how the interface is assembled.
