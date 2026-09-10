# diailectics

Diailectics experiments with Claude

A persona-based Claude chat interface for the Gamson & Modigliani "Runaway"
frame-annotation exercise. Paste a newspaper article; Claude selects the
passages worth annotating against the coding schema, then four personas —
Devil's Advocate, Sycophant, Hegel, Adorno — each comment on every passage,
including their own yes/no stance on whether it carries the Runaway frame.
You can reply to any persona's response and it will update in place, staying
in character.

## Contents

- `tools/claude_chat.py` — the backend: parses the personas out of
  `persona_test_prompts.md`, flattens `runaway_package_annotator_instructions.html`
  into the coding schema, calls Claude, and renders styled HTML output.
- `interface.ipynb` — the interactive notebook front end (ipywidgets).
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

## Running it

```bash
jupyter notebook interface.ipynb
```

Paste an article, pick a model and which personas to run, and click
"Annotate article". Expand a persona's tab to read its full response and
write a reply — it's sent back to that persona as a follow-up turn.

See the module docstring in `tools/claude_chat.py` for the programmatic API
(`ChatBackend.run()` for a single passage, `ChatBackend.annotate_article()`
for a whole article).
