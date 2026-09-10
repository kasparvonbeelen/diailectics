"""
ipywidgets front end for tools/claude_chat.py's ChatBackend - built for
interface.ipynb, which should stay as thin as possible: import, build the
interface, display it.

    from tools.claude_chat import ChatBackend
    from tools.notebook_ui import build_interface

    backend = ChatBackend()
    ui = build_interface(backend)
    display(ui)

Kept in a separate module from tools/claude_chat.py so that module has no
ipywidgets dependency and stays usable headlessly (see its own "Calling it
without the widgets" docs, and the interface.ipynb closing cell).

`build_interface()` returns one widget covering the whole flow: the article
input + controls, the "add a custom passage" form, and a "save your work"
button. Passages from *either* source render the same way and land in the
same growing comments column beside the article - "combine the persona
responses with the custom passages section" means there is only one section,
not two. Every passage ever produced this session is available afterward as
`ui.session_passages` (a list of {"label", "source", "passage"} dicts, "source"
being "article" or "custom") for programmatic/export use beyond the Save
button.
"""

import json
import os
import shutil
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

import ipywidgets as widgets
from IPython.display import display, HTML

from tools.claude_chat import (
    PERSONA_META,
    PERSONA_KEYS,
    CONSENSUS_COLORS,
    API_KEY_JSON_PATH,
    extract_stance,
    one_sentence_summary,
    render_conversation_thread,
    render_article_marks_html,
    serialize_passage,
)

IN_COLAB = "google.colab" in sys.modules
MODEL_OPTIONS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]


def ensure_api_key() -> str:
    """Best-effort interactive credential resolution for notebook use. Checks
    the environment, then (on Colab) a Colab secret named ANTHROPIC_API_KEY,
    then an uploaded api_key.json (staged next to tools/claude_chat.py if
    found elsewhere - e.g. Colab's default /content/ upload location), then -
    on Colab only, as a last resort - prompts interactively. Returns a status
    message to print; never raises for a missing key."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is already set in the environment."

    if IN_COLAB:
        try:
            from google.colab import userdata
            key = userdata.get("ANTHROPIC_API_KEY")
        except Exception:
            key = None
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            return "Loaded ANTHROPIC_API_KEY from Colab secrets (the key icon in the left sidebar)."

    if API_KEY_JSON_PATH.exists():
        return "Found api_key.json - tools/claude_chat.py will read its 'anthropic_api_key' field automatically."
    for candidate_dir in ("/content", os.path.expanduser("~")):
        candidate = Path(candidate_dir) / "api_key.json"
        if candidate.exists():
            shutil.copy(candidate, API_KEY_JSON_PATH)
            return "Found api_key.json - copied next to tools/claude_chat.py."

    if IN_COLAB:
        print("No credentials found yet. Options, in order of preference:")
        print("  1. Add a Colab secret named ANTHROPIC_API_KEY (key icon in the left sidebar), or")
        print("  2. Upload an api_key.json file (with an 'anthropic_api_key' field) into this session, or")
        print("  3. Paste your key below (kept only in memory for this session).")
        entered = getpass("Anthropic API key (leave blank to skip): ").strip()
        if entered:
            os.environ["ANTHROPIC_API_KEY"] = entered
            return "Key set for this session."
        return "No key set - ChatBackend() will fail until one of the options above is provided."

    return (
        "No ANTHROPIC_API_KEY / api_key.json found locally. Set ANTHROPIC_API_KEY in your "
        "shell, add a .env file, or place api_key.json next to tools/claude_chat.py - see README.md."
    )


def _passage_badge_info(passage: dict):
    """(consensus_label, color, motivation) for a passage's badge - how much
    the personas' own stances overlap (see passage_consensus() in
    tools.claude_chat), not just the single up-front provisional guess."""
    label = passage.get("initial_consensus_label") or ("yes" if passage["contains_runaway"] else "no")
    motivation = passage.get("initial_consensus_motivation") or passage.get("justification", "")
    return label, CONSENSUS_COLORS[label], motivation


def _persona_panel_title(persona_key: str, raw_text: str) -> str:
    stance = extract_stance(raw_text)
    icon = "✅" if stance else ("❌" if stance is False else "❔")
    summary = one_sentence_summary(raw_text)
    title = PERSONA_META[persona_key]["title"]
    return f"{icon} {title} — {summary}"


def _handle_reply(accordion, persona_key, conversation, response_html, reply_box, status_html):
    text = reply_box.value.strip()
    if not text:
        return
    status_html.value = "<i>Sending reply to Claude...</i>"
    try:
        new_raw = conversation.reply(text)
    except Exception as exc:
        status_html.value = f'<span style="color:#8B3A3A;">Error: {exc}</span>'
        return
    response_html.value = render_conversation_thread(persona_key, conversation)
    reply_box.value = ""
    # Find this persona's tab by matching its own response widget, so the title
    # (icon + summary) refreshes even if tabs get reordered or rebuilt later.
    for i, child in enumerate(accordion.children):
        if child.children[0] is response_html:
            accordion.set_title(i, _persona_panel_title(persona_key, new_raw))
            break
    status_html.value = f"<i>Updated (reply {conversation.reply_count}).</i>"


def _build_persona_accordion(passage: dict) -> widgets.Accordion:
    """One Accordion, one tab per persona, each with the response + a reply box
    wired to that persona's PersonaConversation."""
    accordion = widgets.Accordion()
    children = []
    entries = list(passage["personas"].items())

    for persona_key, pdata in entries:
        title_text = PERSONA_META[persona_key]["title"]
        response_html = widgets.HTML(value=render_conversation_thread(persona_key, pdata["conversation"]))
        reply_box = widgets.Textarea(
            placeholder=f"Write a reply to {title_text}...",
            layout=widgets.Layout(width="100%", height="70px"),
        )
        send_button = widgets.Button(description="Send reply", button_style="")
        status_html = widgets.HTML(value="")
        reply_label = widgets.HTML(
            f'<div style="margin:10px 0 4px;font-size:12px;color:#5A5C4F;">'
            f"<b>Your reply to {title_text}:</b></div>"
        )
        panel = widgets.VBox([
            response_html,
            reply_label,
            reply_box,
            widgets.HBox([send_button, status_html]),
        ])
        children.append(panel)
        # persona_key/conversation/response_html/reply_box/status_html are captured
        # per-iteration via these default arguments (classic loop-closure fix).
        send_button.on_click(
            lambda _btn, persona_key=persona_key, conversation=pdata["conversation"],
                   response_html=response_html, reply_box=reply_box, status_html=status_html: (
                _handle_reply(accordion, persona_key, conversation, response_html, reply_box, status_html)
            )
        )

    accordion.children = children
    for i, (persona_key, pdata) in enumerate(entries):
        accordion.set_title(i, _persona_panel_title(persona_key, pdata["raw_text"]))
    return accordion


def _build_passage_card(label: str, passage: dict) -> widgets.VBox:
    """One comment card: label, consensus badge (yes/no/maybe) + motivation, a
    quote preview, and that passage's persona accordion. The same card is used
    whether the passage came from the article run or was added by hand -
    that's what makes this one combined section rather than two."""
    consensus_label, badge_color, motivation = _passage_badge_info(passage)
    quote = passage["quote"]
    preview = quote if len(quote) <= 110 else quote[:107] + "..."
    header_html = (
        f'<div style="font-family:Georgia,\'Lora\',serif;padding-top:10px;'
        f'border-top:2px solid #C9C4B4;margin-top:14px;">'
        f'<div style="font-family:\'JetBrains Mono\',monospace;font-size:11px;'
        f'text-transform:uppercase;letter-spacing:.05em;color:#5A5C4F;margin-bottom:6px;">'
        f'{label}</div>'
        f'<span style="display:inline-block;font-size:11px;font-family:\'JetBrains Mono\',monospace;'
        f'text-transform:uppercase;letter-spacing:.05em;color:#fff;background:{badge_color};'
        f'padding:2px 8px;border-radius:10px;">Runaway: {consensus_label}</span>'
        f'<div style="font-style:italic;color:#5A5C4F;font-size:12.5px;margin:6px 0 4px;">'
        f'&ldquo;{preview}&rdquo;</div>'
        f'<div style="color:#5A5C4F;font-size:12.5px;">{motivation}</div>'
        f'</div>'
    )
    return widgets.VBox([widgets.HTML(header_html), _build_persona_accordion(passage)])


def build_interface(backend, model_options=None) -> widgets.Widget:
    """Build the whole interface as one widget: article input + controls, a
    custom-passage form, a save button, and one combined display area (the
    article on the left, every passage's comments - from either source - in a
    single growing column on the right). Returns the root widget to display();
    the full passage history is available afterward as `<returned>.session_passages`.
    """
    model_options = model_options or MODEL_OPTIONS

    # --- main article-run controls ---
    article_box = widgets.Textarea(
        value="",
        placeholder="Paste the full article here...",
        description="Article:",
        layout=widgets.Layout(width="100%", height="220px"),
        style={"description_width": "60px"},
    )
    model_box = widgets.Combobox(
        value=backend.model,
        placeholder="Model ID",
        options=model_options,
        description="Model:",
        ensure_option=False,
        style={"description_width": "60px"},
    )
    max_passages_slider = widgets.IntSlider(
        value=4, min=1, max=8, step=1, description="Max passages:",
        style={"description_width": "100px"},
    )
    persona_checkboxes = {
        key: widgets.Checkbox(value=True, description=PERSONA_META[key]["title"])
        for key in PERSONA_KEYS
    }
    run_button = widgets.Button(description="Annotate article", button_style="primary")
    show_raw_checkbox = widgets.Checkbox(value=False, description="Show raw selection output")
    run_status_output = widgets.Output()

    # --- custom-passage controls ---
    custom_passage_box = widgets.Textarea(
        value="",
        placeholder="Paste or type a specific passage you want persona feedback on...",
        description="Passage:",
        layout=widgets.Layout(width="100%", height="100px"),
        style={"description_width": "60px"},
    )
    custom_runaway_toggle = widgets.ToggleButtons(
        options=[("Yes — Runaway frame present", True), ("No — Runaway frame not present", False)],
        description="Your annotation:",
        style={"description_width": "120px"},
    )
    custom_justification_box = widgets.Text(
        value="",
        placeholder="Optional: why you think so (passed to the personas as your justification)",
        layout=widgets.Layout(width="100%"),
        style={"description_width": "60px"},
    )
    custom_persona_checkboxes = {
        key: widgets.Checkbox(value=True, description=PERSONA_META[key]["title"])
        for key in PERSONA_KEYS
    }
    custom_run_button = widgets.Button(description="Get persona feedback", button_style="primary")
    custom_status_html = widgets.HTML(value="")

    # --- shared display area: article on the left, one growing comments
    # column on the right, fed by both the article run and custom passages ---
    article_widget = widgets.HTML(value="")
    comments_column = widgets.VBox([])
    display_area = widgets.HBox(
        [article_widget, comments_column],
        layout=widgets.Layout(align_items="flex-start", width="100%", flex_flow="row wrap"),
    )

    # --- save button ---
    save_button = widgets.Button(description="Save annotations", button_style="success")
    save_status_html = widgets.HTML(value="")

    session_passages = []
    passage_counter = [0]
    custom_counter = [0]

    def add_card(label, passage, source):
        card = _build_passage_card(label, passage)
        comments_column.children = comments_column.children + (card,)
        session_passages.append({"label": label, "source": source, "passage": passage})

    def on_run_clicked(_):
        run_status_output.clear_output()
        with run_status_output:
            if not article_box.value.strip():
                print("Paste an article first.")
                return
            selected_personas = [key for key, box in persona_checkboxes.items() if box.value]
            if not selected_personas:
                print("Select at least one persona.")
                return
            n_calls = 1 + max_passages_slider.value * len(selected_personas)
            print(f"Calling Claude ({model_box.value})... up to {n_calls} API calls, running concurrently.")
            result = backend.annotate_article(
                article_box.value.strip(),
                personas=selected_personas,
                max_passages=max_passages_slider.value,
                model=model_box.value,
            )
            article_widget.value = render_article_marks_html(article_box.value.strip(), result["passages"])
            run_status_output.clear_output()
            print(f"Found {len(result['passages'])} passage(s). Comments appear beside the article, on the right.")
            if show_raw_checkbox.value:
                print("\n--- raw passage-selection output ---\n")
                print(result["selection_raw"])
            for passage in result["passages"]:
                passage_counter[0] += 1
                add_card(f"Passage {passage_counter[0]}", passage, "article")

    def on_custom_run_clicked(_):
        text = custom_passage_box.value.strip()
        if not text:
            custom_status_html.value = "<i>Enter a passage first.</i>"
            return
        selected_personas = [key for key, box in custom_persona_checkboxes.items() if box.value]
        if not selected_personas:
            custom_status_html.value = "<i>Select at least one persona.</i>"
            return
        custom_status_html.value = (
            f"<i>Calling Claude ({model_box.value}) for {len(selected_personas)} persona(s)...</i>"
        )
        passage = backend.run_passage(
            text,
            personas=selected_personas,
            contains_runaway=custom_runaway_toggle.value,
            justification=custom_justification_box.value.strip(),
            model=model_box.value,
        )
        custom_counter[0] += 1
        label = f"Custom passage {custom_counter[0]}"
        custom_status_html.value = f"<i>Done ({label}) - added to the comments column above.</i>"
        add_card(label, passage, "custom")

    def on_save_clicked(_):
        if not session_passages:
            save_status_html.value = "<i>Nothing to save yet - run an annotation or add a custom passage first.</i>"
            return
        payload = {
            "saved_at": datetime.now().astimezone().isoformat(),
            "model": model_box.value,
            "article_text": article_box.value,
            "entries": [
                {"label": entry["label"], "source": entry["source"], **serialize_passage(entry["passage"])}
                for entry in session_passages
            ],
        }
        filename = f"diailectics_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        message = f"Saved {len(session_passages)} passage(s) to <code>{filename}</code>"
        if IN_COLAB:
            try:
                from google.colab import files
                files.download(filename)
                message += " (browser download started)."
            except Exception as exc:
                message += f" (couldn't trigger an automatic download: {exc} - find the file in the Colab file browser)."
        else:
            message += f" in {os.getcwd()}."
        save_status_html.value = f"<i>{message}</i>"

    run_button.on_click(on_run_clicked)
    custom_run_button.on_click(on_custom_run_clicked)
    save_button.on_click(on_save_clicked)

    root = widgets.VBox([
        widgets.HTML("<h3 style=\"margin-bottom:2px;\">Annotate an article</h3>"),
        article_box,
        model_box,
        max_passages_slider,
        widgets.HTML("<b>Personas:</b>"),
        widgets.HBox(list(persona_checkboxes.values())),
        widgets.HBox([run_button, show_raw_checkbox]),
        run_status_output,
        display_area,
        widgets.HTML(
            "<h3 style=\"margin-bottom:2px;\">Add a custom passage</h3>"
            "<p style=\"font-size:12.5px;color:#5A5C4F;margin-top:0;\">For text the automatic "
            "selection above didn't flag as important - paste your own passage, pick which "
            "persona(s) should respond, and give your own yes/no annotation. It's added to the "
            "same comments column above, alongside the article's own passages.</p>"
        ),
        custom_passage_box,
        custom_runaway_toggle,
        custom_justification_box,
        widgets.HTML("<b>Personas:</b>"),
        widgets.HBox(list(custom_persona_checkboxes.values())),
        widgets.HBox([custom_run_button, custom_status_html]),
        widgets.HTML(
            "<h3 style=\"margin-bottom:2px;\">Save your work</h3>"
            "<p style=\"font-size:12.5px;color:#5A5C4F;margin-top:0;\">Writes every passage "
            "annotated so far (from the article run and any custom passages) to a timestamped "
            "JSON file - the full transparent record for each: the original text, the "
            "provisional annotation, and every persona's complete response history together "
            "with the replies that produced each update, not just the latest state.</p>"
        ),
        widgets.HBox([save_button, save_status_html]),
    ])
    root.session_passages = session_passages
    return root
