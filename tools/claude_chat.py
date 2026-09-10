"""
Persona-based Claude chat backend for the Runaway-frame annotation exercise.

Given a persona from persona_test_prompts.md (Devil's Advocate, Sycophant, Hegel,
Adorno) and either (a) a single passage + a yes/no Runaway-frame annotation, or
(b) a whole article, this module calls Claude (stateless mode - no session memory,
matching the "[STATELESS MODE ONLY: ...]" branch of each persona prompt) and returns
both the raw tagged response and a styled HTML rendering that reuses the color
palette from runaway_package_annotator_instructions.html.

Two entry points on ChatBackend:

    from tools.claude_chat import ChatBackend

    backend = ChatBackend()  # reads persona_test_prompts.md + the instructions HTML

    # (a) one passage, one persona, a yes/no annotation you already have
    result = backend.run(text="...", persona="hegel", contains_runaway=True)
    result["html"]; result["raw_text"]

    # (b) a whole article: Claude first selects the important passages itself
    #     (using the schema, not asked yes/no by the caller), then every requested
    #     persona comments on each one - including its own <stance> (yes/no) badge.
    result = backend.annotate_article(article_text, model="claude-opus-5")
    result["html"]       # full article with highlighted passages + persona comments
    result["passages"]   # structured list: quote, provisional annotation, per-persona output

Every persona response carries a <stance>yes|no</stance> tag (rendered as a badge)
alongside its <point>/<quote>/<conclusion> content - see extract_stance().

Each persona output also comes with a "conversation" (a PersonaConversation): call
`conversation.reply("...")` to send the annotator's reply and get the persona's
updated response, in the same role, with the schema and full history still in
context (see interface.ipynb for the interactive per-persona reply widgets).

Every call accepts an optional `model=` override (defaults to the model ChatBackend
was constructed with, itself defaulting to DEFAULT_MODEL) - see
`shared/live-sources.md`-listed model IDs in the claude-api skill for current options
(e.g. "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5").

annotate_article() makes 1 + (passages x personas) API calls up front, run
concurrently in a small thread pool - e.g. 4 passages x 4 personas = 17 calls. Tune
`max_passages` and `personas` to control cost. Each reply() afterward is one more
call, on top of that.

Credentials are resolved in this order: an explicit `client` passed to
ChatBackend, then ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in the environment
(or a .env file in the project root), then the "anthropic_api_key" field of
api_key.json in the project root, then whatever `anthropic.Anthropic()` finds on
its own (an `ant auth login` profile, WIF, ...). This module never asks for,
prints, or hardcodes the key itself - api_key.json holds a live secret, so keep
it out of anything you publish or share.
"""

import html as html_lib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - bs4 is in requirements, this is just a safety net
    BeautifulSoup = None

try:
    from langsmith import traceable
except ImportError:  # pragma: no cover - tracing is optional
    def traceable(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator if not (args and callable(args[0])) else args[0]

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONA_PROMPTS_PATH = REPO_ROOT / "persona_test_prompts.md"
RUNAWAY_INSTRUCTIONS_PATH = REPO_ROOT / "runaway_package_annotator_instructions.html"
API_KEY_JSON_PATH = REPO_ROOT / "api_key.json"


def _load_api_key_from_json(path: Path = API_KEY_JSON_PATH):
    """Read the 'anthropic_api_key' field out of api_key.json, if it exists."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("anthropic_api_key")


def _make_client() -> anthropic.Anthropic:
    """Build the Anthropic client, falling back to api_key.json when no
    ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN is set in the environment."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return anthropic.Anthropic()
    key = _load_api_key_from_json()
    if key:
        return anthropic.Anthropic(api_key=key)
    return anthropic.Anthropic()  # falls through to `ant auth login` / WIF, or errors clearly


DEFAULT_MODEL = "claude-opus-5"
DEFAULT_RESEARCH_QUESTION = (
    "Does this passage draw on Gamson & Modigliani's 'Runaway' interpretive package "
    "(nuclear technology as a force that has slipped, or is at constant risk of "
    "slipping, outside human control), and how should the provisional yes/no "
    "annotation of this passage be understood in relation to the text?"
)

# Colors lifted directly from runaway_package_annotator_instructions.html's CSS
# variables, so persona output visually matches the instructions document.
PAPER_RAISED = "#F7F5EF"
INK = "#24261F"
INK_SOFT = "#5A5C4F"
RULE = "#C9C4B4"

PERSONA_META = {
    "devils_advocate": {"title": "Devil's Advocate", "color": "#8B3A3A"},
    "sycophant": {"title": "Sycophant", "color": "#4A7A6B"},
    "hegel": {"title": "Hegel", "color": "#3D5A80"},
    "adorno": {"title": "Adorno", "color": "#6B4E8E"},
}
PERSONA_KEYS = list(PERSONA_META.keys())

# The "Task:" line from each persona's input template in persona_test_prompts.md.
# Adorno's is adapted: its original template takes a *list* of outlier cases and
# examines tensions across them; this interface hands it a single text + a single
# annotation, so it is scoped down to "does this one case expose a tension" rather
# than a cross-case comparison. That deviation from the source prompt is deliberate
# and documented here rather than silently applied.
TASK_LINES = {
    "devils_advocate": "Oppose this annotation.",
    "sycophant": (
        "Extend this annotation without contradicting it. State clearly whether "
        "your extension confirms the annotation or exposes an overlooked implication."
    ),
    "hegel": (
        "Propose a genuinely alternative interpretation, and describe what an "
        "adjusted annotation incorporating both readings might look like."
    ),
    "adorno": (
        "Treating this single passage and its provisional annotation as one outlier "
        "case, identify the internal contradiction(s) or unresolved tension(s) in the "
        "schema that it exposes. Do not propose a resolution."
    ),
}

OUTPUT_FORMAT_INSTRUCTIONS = """
OUTPUT FORMAT (follow exactly - no HTML, no Markdown, no other tags):
Write your response as plain text using only these tags, never nested:
- <point>...</point> wraps one discrete argument, observation, or step of your reasoning.
- <quote>...</quote> wraps an exact, verbatim excerpt from the document text (original
  language), offered as evidence.
- <stance>yes</stance> or <stance>no</stance> - exactly one. This is a structured data
  field for the interface (rendered as a badge), not a recommendation to the
  annotator: state how your own reasoning above would classify this passage on the
  Runaway frame. It does not override any rule above about not telling the annotator
  what to conclude - it labels your argument, it does not instruct them.
- <conclusion>...</conclusion> wraps exactly one closing block, at the end of your
  response, containing whatever your role's rules above require as a closing
  statement (a final counter-claim, a confirm/tension statement, a proposed
  adjustment, or a stated tension - never a verdict you have been told is not yours
  to give).
Use as many <point> and <quote> tags as needed, in the order they occur in your
reasoning, followed by exactly one <stance> tag and exactly one <conclusion> tag,
in that order. No text outside these tags.

If the annotator replies to your response, stay in this same persona role (do not
become a generic assistant) and answer using this same tag format.
""".strip()

_TAG_RE = re.compile(r"<(point|quote|stance|conclusion)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_MEMORY_BRACKET_RE = re.compile(r"\[MEMORY MODE ONLY:.*?\]", re.DOTALL | re.IGNORECASE)
_STATELESS_BRACKET_RE = re.compile(r"\[STATELESS MODE ONLY:\s*(.*?)\]", re.DOTALL | re.IGNORECASE)
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_PERSONA_HEADER_RE = re.compile(r"^##\s*\d+\.\s*(.+?)\s*$", re.MULTILINE)


def _slugify_persona_title(title: str) -> str:
    lowered = title.lower()
    if "devil" in lowered:
        return "devils_advocate"
    if "sycophant" in lowered:
        return "sycophant"
    if "hegel" in lowered:
        return "hegel"
    if "adorno" in lowered:
        return "adorno"
    raise ValueError(f"Unrecognized persona heading: {title!r}")


def _stateless_instruction(raw_instruction: str) -> str:
    """Strip memory-mode guidance, keep only the stateless-mode variant."""
    text = _MEMORY_BRACKET_RE.sub("", raw_instruction)
    text = _STATELESS_BRACKET_RE.sub(lambda m: m.group(1).strip(), text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def load_personas(md_path: Path = PERSONA_PROMPTS_PATH) -> dict:
    """Parse persona_test_prompts.md into {persona_key: stateless instruction text}."""
    text = Path(md_path).read_text(encoding="utf-8")
    sections = re.split(r"\n(?=##\s*\d+\.)", text)
    personas = {}
    for section in sections:
        header = _PERSONA_HEADER_RE.match(section)
        if not header:
            continue
        key = _slugify_persona_title(header.group(1))
        instr_match = re.search(
            r"\*\*Persona instruction:\*\*\s*```\s*(.*?)```", section, re.DOTALL
        )
        if not instr_match:
            continue
        personas[key] = _stateless_instruction(instr_match.group(1))
    missing = set(PERSONA_KEYS) - set(personas)
    if missing:
        raise ValueError(f"Could not find persona instruction(s) for: {sorted(missing)}")
    return personas


def load_runaway_schema(html_path: Path = RUNAWAY_INSTRUCTIONS_PATH) -> str:
    """Flatten the annotator-instructions HTML into plain text for the prompt."""
    raw_html = Path(html_path).read_text(encoding="utf-8")
    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    else:  # pragma: no cover - fallback if bs4 isn't installed
        text = re.sub(r"<style.*?</style>", "", raw_html, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = html_lib.unescape(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def parse_tagged_output(raw_text: str):
    """Return [(tag, content), ...] in document order."""
    return [(m.group(1).lower(), m.group(2).strip()) for m in _TAG_RE.finditer(raw_text)]


_SENTENCE_END_RE = re.compile(r"^(.*?[.!?])(\s|$)")


def extract_stance(raw_text: str):
    """Return True/False for the persona's <stance> tag, or None if absent
    (e.g. the model didn't follow the tag format)."""
    nodes = parse_tagged_output(raw_text)
    for tag, content in nodes:
        if tag == "stance":
            return content.strip().lower().startswith("y")
    return None


def _stance_badge_html(stance: bool) -> str:
    badge_color = "#4A7A6B" if stance else "#8B3A3A"
    badge_text = "Runaway: yes" if stance else "Runaway: no"
    return (
        f'<span style="display:inline-block;font-size:10.5px;'
        f"font-family:'JetBrains Mono','Courier New',monospace;text-transform:uppercase;"
        f'letter-spacing:.05em;color:#fff;background:{badge_color};'
        f'padding:2px 8px;border-radius:10px;margin-bottom:8px;">{badge_text}</span>'
    )


def render_persona_body(raw_text: str, color: str) -> str:
    """Inner HTML (no outer card) for one persona's tagged output: a stance badge
    (if present) followed by the <point>/<quote>/<conclusion> content."""
    nodes = parse_tagged_output(raw_text)
    if not nodes:
        # Model didn't follow the tag format - fall back to plain escaped text.
        escaped = html_lib.escape(raw_text).replace("\n", "<br>")
        return f'<p style="margin:0;">{escaped}</p>'

    stance_html = ""
    parts = []
    for tag, content in nodes:
        if tag == "stance":
            stance_html = _stance_badge_html(content.strip().lower().startswith("y"))
            continue
        escaped = html_lib.escape(content).replace("\n", "<br>")
        if tag == "quote":
            parts.append(
                f'<blockquote style="margin:8px 0;padding:8px 16px;'
                f'border-left:3px solid {color};background:#fff;border-radius:4px;'
                f'font-style:italic;">{escaped}</blockquote>'
            )
        elif tag == "point":
            parts.append(f'<p style="margin:0 0 10px;">{escaped}</p>')
        elif tag == "conclusion":
            parts.append(
                f'<div style="margin-top:14px;padding-top:10px;'
                f'border-top:1px solid {RULE};font-weight:600;">{escaped}</div>'
            )
    body = "\n".join(parts)
    return f"{stance_html}{body}" if stance_html else body


def one_sentence_summary(raw_text: str, max_len: int = 160) -> str:
    """A short summary for a persona's response, pulled from its <conclusion> tag
    (that tag is already defined as the persona's required closing statement)."""
    nodes = parse_tagged_output(raw_text)
    conclusion = next((content for tag, content in nodes if tag == "conclusion"), None)
    source = " ".join((conclusion or raw_text).split())
    match = _SENTENCE_END_RE.match(source)
    sentence = match.group(1) if match else source
    if len(sentence) > max_len:
        sentence = sentence[: max_len - 1].rstrip() + "…"
    return sentence


def render_html(persona_key: str, raw_text: str) -> str:
    """Render a persona's tagged response as styled HTML matching the instructions doc."""
    meta = PERSONA_META[persona_key]
    color, title = meta["color"], meta["title"]
    body = render_persona_body(raw_text, color)

    return f"""<div style="border:1px solid {RULE};border-left:5px solid {color};
border-radius:6px;padding:16px 20px;margin:16px 0;max-width:800px;
font-family:Georgia,'Lora',serif;background:{PAPER_RAISED};color:{INK};">
  <div style="font-family:'JetBrains Mono','Courier New',monospace;font-size:11px;
letter-spacing:0.05em;text-transform:uppercase;color:{INK_SOFT};margin-bottom:10px;">
    {html_lib.escape(title)}
  </div>
  {body}
</div>"""


def build_messages(
    personas: dict,
    schema: str,
    text: str,
    persona_key: str,
    contains_runaway: bool,
    research_question: str = DEFAULT_RESEARCH_QUESTION,
    annotation_text: str = None,
):
    """Build (system, user) strings for one stateless persona call.

    `annotation_text` overrides the auto-generated "frame_label + justification"
    string (used by annotate_article() to pass through the justification Claude's
    own passage-selection step already produced, instead of a generic sentence).
    """
    if persona_key not in personas:
        raise ValueError(f"Unknown persona {persona_key!r}; choose from {PERSONA_KEYS}")

    system = "\n\n".join([
        personas[persona_key],
        OUTPUT_FORMAT_INSTRUCTIONS,
        f"Research question: {research_question}",
        f"Coding schema (current version):\n{schema}",
    ])

    if annotation_text is None:
        annotation_label = "Runaway frame PRESENT" if contains_runaway else "Runaway frame NOT PRESENT"
        justification = (
            "The human annotator judged this passage draws on the Runaway interpretive package."
            if contains_runaway
            else "The human annotator judged this passage does not draw on the Runaway interpretive package."
        )
        annotation_text = f"{annotation_label} — {justification}"

    unit_label = "Outlier case" if persona_key == "adorno" else "Document / annotation unit"

    user = (
        f"{unit_label}: {text}\n\n"
        f"Human's provisional annotation: {annotation_text}\n\n"
        f"Task: {TASK_LINES[persona_key]}"
    )
    return system, user


class PersonaConversation:
    """One persona's ongoing exchange about one passage. Starts stateless (a
    single call, per persona_test_prompts.md's "[STATELESS MODE ONLY: ...]"
    variant), but once the annotator writes a reply, reply() turns it into a
    normal multi-turn conversation - the persona's own instruction, schema, and
    output format (`system`) never change, only `messages` grows, so the cached
    system prefix keeps paying off on every follow-up call."""

    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str,
        system: str,
        messages: list,
        persona_key: str,
        raw_text: str,
        usage,
    ):
        self.client = client
        self.model = model
        self.system = system
        self.messages = messages  # turns sent so far (not yet including raw_text)
        self.persona_key = persona_key
        self.raw_text = raw_text
        self.history = [raw_text]  # every response so far, oldest first
        self.usages = [usage]

    @property
    def reply_count(self) -> int:
        return len(self.history) - 1

    def reply(self, user_text: str, max_tokens: int = 4096) -> str:
        """Send the annotator's reply; returns the persona's updated raw_text."""
        self.messages = self.messages + [
            {"role": "assistant", "content": self.raw_text},
            {"role": "user", "content": user_text},
        ]
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            messages=self.messages,
        )
        raw_text = "".join(block.text for block in response.content if block.type == "text")
        self.raw_text = raw_text
        self.history.append(raw_text)
        self.usages.append(response.usage)
        return raw_text


def start_persona_conversation(
    client: anthropic.Anthropic,
    personas: dict,
    schema: str,
    text: str,
    persona_key: str,
    contains_runaway: bool,
    model: str,
    research_question: str = DEFAULT_RESEARCH_QUESTION,
    annotation_text: str = None,
    max_tokens: int = 4096,
) -> PersonaConversation:
    """One stateless persona API call, wrapped so it can be replied to later."""
    system, user = build_messages(
        personas, schema, text, persona_key, contains_runaway, research_question, annotation_text
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": user}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    return PersonaConversation(
        client, model, system, [{"role": "user", "content": user}], persona_key, raw_text, response.usage
    )


# --- Whole-article passage selection ---------------------------------------
#
# annotate_article() doesn't ask the caller for a yes/no annotation. Instead a
# first Claude call plays the annotator's role: read the whole article against
# the schema, pick out only the passages worth commenting on (most of a
# newspaper article is routine reporting that no persona needs to see), and
# produce a provisional yes/no + justification for each - the same
# "frame_label + justification" that persona_test_prompts.md's input template
# expects a human to supply. Each persona then reacts to that passage exactly
# as in the single-passage flow.

PASSAGE_SELECTION_FORMAT = """
OUTPUT FORMAT (follow exactly - no HTML, no Markdown, no other tags):
For each passage you select, output one block:
<passage>
<quote>the passage, copied VERBATIM from the article below - same wording, \
punctuation, and language, so it can be located in the source text</quote>
<annotation>yes</annotation> (or <annotation>no</annotation> - does this passage \
draw on the Runaway frame?)
<justification>1-3 sentences justifying that annotation, grounded in the schema's \
framing/reasoning devices</justification>
</passage>
Output between 1 and {max_passages} <passage> blocks, most important first. Skip
routine reporting entirely - procedural detail, unrelated political news,
logistics - rather than force a weak passage in just to fill the quota. No text
outside <passage> blocks.
""".strip()

_PASSAGE_BLOCK_RE = re.compile(r"<passage>(.*?)</passage>", re.DOTALL | re.IGNORECASE)
_PASSAGE_FIELD_RE = re.compile(
    r"<(quote|annotation|justification)>(.*?)</\1>", re.DOTALL | re.IGNORECASE
)


def select_important_passages(
    client: anthropic.Anthropic,
    schema: str,
    article_text: str,
    model: str,
    max_passages: int = 4,
    research_question: str = DEFAULT_RESEARCH_QUESTION,
    max_tokens: int = 4096,
):
    """Ask Claude to pick out the passages of `article_text` worth annotating.

    Returns (passages, raw_text, usage) where passages is a list of
    {"quote", "contains_runaway", "justification"} dicts, most important first.
    """
    system = "\n\n".join([
        "You are annotating a newspaper article for Gamson & Modigliani's "
        '"Runaway" interpretive package, using the schema below. Be selective: '
        "most of a newspaper article is not about this frame at all.",
        PASSAGE_SELECTION_FORMAT.format(max_passages=max_passages),
        f"Research question: {research_question}",
        f"Coding schema (current version):\n{schema}",
    ])
    user = f"Article:\n{article_text}"
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": user}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    return parse_passages(raw_text), raw_text, response.usage


def parse_passages(raw_text: str):
    """Parse select_important_passages()'s tagged output into structured dicts."""
    passages = []
    for block_match in _PASSAGE_BLOCK_RE.finditer(raw_text):
        fields = {
            tag.lower(): content.strip()
            for tag, content in _PASSAGE_FIELD_RE.findall(block_match.group(1))
        }
        if not fields.get("quote"):
            continue
        passages.append({
            "quote": fields["quote"],
            "contains_runaway": fields.get("annotation", "").strip().lower().startswith("y"),
            "justification": fields.get("justification", ""),
        })
    return passages


# --- Hover-card article rendering -------------------------------------------
#
# Important passages are highlighted inline. Hovering (or focusing, for keyboard
# use) a highlighted passage opens a small card listing every persona's one-
# sentence summary (pulled from its <conclusion> tag); clicking a summary expands
# it in place into the full <point>/<quote>/<conclusion> response. Pure CSS
# (:hover/:focus-within + native <details>/<summary>) - no JS dependency, so it
# renders the same in Jupyter classic, JupyterLab, and the VS Code notebook
# renderer.

ARTICLE_STYLE = f"""<style>
.rw-article-wrap {{ max-width:820px; font-family:Georgia,'Lora',serif; color:{INK}; }}
.rw-article-body {{ padding:16px 20px; background:{PAPER_RAISED}; border:1px solid {RULE};
  border-radius:6px; line-height:1.7; }}
.rw-passage {{ position:relative; display:inline-block; cursor:help;
  background:#FCE8B2; padding:0 2px; border-radius:2px; border-bottom:2px dotted #9C6B2E; }}
.rw-index {{ font-size:10px; vertical-align:super; color:#9C6B2E; margin-left:1px; }}
.rw-panel {{ display:none; position:absolute; z-index:1000; top:100%; left:0;
  width:420px; max-width:min(420px, 90vw); background:{PAPER_RAISED};
  border:1px solid {RULE}; border-radius:8px; box-shadow:0 6px 20px rgba(0,0,0,.18);
  padding:12px 14px; font-size:13px; line-height:1.5; color:{INK}; cursor:auto; }}
.rw-passage:hover .rw-panel, .rw-passage:focus .rw-panel,
.rw-passage:focus-within .rw-panel, .rw-panel:hover {{ display:block; }}
.rw-panel-header {{ font-family:'JetBrains Mono','Courier New',monospace; font-size:11px;
  text-transform:uppercase; letter-spacing:.05em; color:{INK_SOFT}; margin-bottom:6px; }}
.rw-panel-justification {{ font-style:italic; color:{INK_SOFT}; font-size:12.5px; margin:0 0 10px; }}
.rw-persona-item {{ border-left:4px solid var(--persona-color); background:#fff;
  border-radius:4px; margin-bottom:6px; padding:6px 10px 8px; }}
.rw-persona-item summary {{ cursor:pointer; font-size:12.5px; line-height:1.4; }}
.rw-persona-full {{ margin-top:8px; font-size:12.5px; }}
.rw-unmatched {{ margin-top:22px; padding-top:12px; border-top:1px solid {RULE}; }}
.rw-unmatched-title {{ font-family:'JetBrains Mono','Courier New',monospace; font-size:11px;
  text-transform:uppercase; letter-spacing:.05em; color:{INK_SOFT}; margin-bottom:8px; }}
</style>"""


def _persona_hover_item(persona_key: str, raw_text: str) -> str:
    meta = PERSONA_META[persona_key]
    color, title = meta["color"], meta["title"]
    summary = html_lib.escape(one_sentence_summary(raw_text))
    body = render_persona_body(raw_text, color)
    return f"""<details class="rw-persona-item" style="--persona-color:{color};">
  <summary><span style="color:{color};font-weight:600;">{html_lib.escape(title)}:</span> {summary}</summary>
  <div class="rw-persona-full">{body}</div>
</details>"""


def _passage_panel_html(idx: int, passage: dict) -> str:
    badge_color = "#4A7A6B" if passage["contains_runaway"] else "#8B3A3A"
    badge_text = "Runaway: yes" if passage["contains_runaway"] else "Runaway: no"
    justification_escaped = html_lib.escape(passage.get("justification", ""))
    persona_items = "\n".join(
        _persona_hover_item(key, p["raw_text"]) for key, p in passage.get("personas", {}).items()
    )
    return f"""<div class="rw-panel" role="tooltip">
  <div class="rw-panel-header">Passage {idx} &middot; <span style="color:{badge_color};">{badge_text}</span></div>
  <div class="rw-panel-justification">{justification_escaped}</div>
  {persona_items}
</div>"""


def render_article_marks_html(article_text: str, passages: list) -> str:
    """Article text with important passages highlighted (index-labelled), no
    popovers - a lightweight static preview to display above the interactive,
    reply-capable per-passage/persona widgets (see interface.ipynb)."""
    escaped_article = html_lib.escape(article_text).replace("\n", "<br>")
    for idx, passage in enumerate(passages, start=1):
        quote_escaped = html_lib.escape(passage["quote"]).replace("\n", "<br>")
        if quote_escaped and quote_escaped in escaped_article:
            marker = (
                f'<mark style="background:#FCE8B2;padding:1px 2px;border-radius:2px;">'
                f'{quote_escaped}<sup style="color:#9C6B2E;">[{idx}]</sup></mark>'
            )
            escaped_article = escaped_article.replace(quote_escaped, marker, 1)
    return (
        f'<div style="max-width:820px;font-family:Georgia,\'Lora\',serif;color:{INK};'
        f'padding:16px 20px;background:{PAPER_RAISED};border:1px solid {RULE};'
        f'border-radius:6px;line-height:1.7;">{escaped_article}</div>'
    )


def render_article_html(article_text: str, passages: list) -> str:
    """Render the article with important passages highlighted inline. Hovering
    (or tabbing to) a highlighted passage opens a card with every persona's
    one-sentence summary; clicking a summary expands the full response."""
    # Convert newlines to <br> in the plain article text *before* splicing in any
    # marker/panel HTML - otherwise this same replace would also mangle the
    # newlines inside the (multi-line, for readability) marker templates below.
    escaped_article = html_lib.escape(article_text).replace("\n", "<br>")
    matched = set()
    for idx, passage in enumerate(passages, start=1):
        quote_escaped = html_lib.escape(passage["quote"]).replace("\n", "<br>")
        if quote_escaped and quote_escaped in escaped_article:
            marker = (
                f'<span class="rw-passage" tabindex="0">{quote_escaped}'
                f'<span class="rw-index">[{idx}]</span>'
                f'{_passage_panel_html(idx, passage)}'
                f'</span>'
            )
            escaped_article = escaped_article.replace(quote_escaped, marker, 1)
            matched.add(idx)
    article_html = escaped_article

    unmatched_html = ""
    unmatched = [(idx, p) for idx, p in enumerate(passages, start=1) if idx not in matched]
    if unmatched:
        # The model didn't copy this quote verbatim, so it can't be highlighted
        # inline - still expose it (and its persona comments) the same way.
        items = "\n".join(
            f'<span class="rw-passage" tabindex="0">&ldquo;{html_lib.escape(p["quote"])}&rdquo;'
            f'<span class="rw-index">[{idx}]</span>'
            f'{_passage_panel_html(idx, p)}</span><br><br>'
            for idx, p in unmatched
        )
        unmatched_html = f"""<div class="rw-unmatched">
  <div class="rw-unmatched-title">Not located verbatim in the article text above (hover for comments)</div>
  {items}
</div>"""

    return f"""{ARTICLE_STYLE}
<div class="rw-article-wrap">
  <div class="rw-article-body">{article_html}</div>
  {unmatched_html}
</div>"""


class ChatBackend:
    """Loads personas + schema once, then answers persona-annotation calls against Claude."""

    def __init__(
        self,
        client: anthropic.Anthropic = None,
        persona_md_path: Path = PERSONA_PROMPTS_PATH,
        instructions_html_path: Path = RUNAWAY_INSTRUCTIONS_PATH,
        model: str = DEFAULT_MODEL,
    ):
        self.client = client or _make_client()
        self.personas = load_personas(persona_md_path)
        self.schema = load_runaway_schema(instructions_html_path)
        self.model = model

    @traceable(name="persona_runaway_chat")
    def run(
        self,
        text: str,
        persona: str,
        contains_runaway: bool,
        research_question: str = DEFAULT_RESEARCH_QUESTION,
        model: str = None,
        max_tokens: int = 4096,
    ) -> dict:
        """Single passage, single persona, caller-supplied yes/no annotation.
        The returned "conversation" can be used to send a reply (see
        PersonaConversation.reply)."""
        conversation = start_persona_conversation(
            self.client, self.personas, self.schema, text, persona, contains_runaway,
            model or self.model, research_question, max_tokens=max_tokens,
        )
        return {
            "persona": persona,
            "raw_text": conversation.raw_text,
            "html": render_html(persona, conversation.raw_text),
            "usage": conversation.usages[-1],
            "conversation": conversation,
        }

    @traceable(name="persona_runaway_annotate_article")
    def annotate_article(
        self,
        article_text: str,
        personas: list = None,
        max_passages: int = 4,
        model: str = None,
        research_question: str = DEFAULT_RESEARCH_QUESTION,
        max_workers: int = 4,
    ) -> dict:
        """Select the important passages in `article_text`, then have every
        requested persona (default: all four) comment on each one.

        Makes 1 + (passages found x len(personas)) API calls - the persona calls
        run concurrently in a thread pool sized by `max_workers`.
        """
        model = model or self.model
        persona_keys = list(personas) if personas else list(PERSONA_KEYS)
        unknown = set(persona_keys) - set(PERSONA_KEYS)
        if unknown:
            raise ValueError(f"Unknown persona(s) {sorted(unknown)}; choose from {PERSONA_KEYS}")

        passages, selection_raw, selection_usage = select_important_passages(
            self.client, self.schema, article_text, model, max_passages, research_question
        )

        conversations = {}  # (passage_idx, persona_key) -> PersonaConversation
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for p_idx, passage in enumerate(passages):
                annotation_text = (
                    ("Runaway frame PRESENT" if passage["contains_runaway"] else "Runaway frame NOT PRESENT")
                    + (f" — {passage['justification']}" if passage["justification"] else "")
                )
                for persona_key in persona_keys:
                    future = pool.submit(
                        start_persona_conversation,
                        self.client, self.personas, self.schema, passage["quote"],
                        persona_key, passage["contains_runaway"], model,
                        research_question, annotation_text,
                    )
                    futures[future] = (p_idx, persona_key)
            for future in as_completed(futures):
                conversations[futures[future]] = future.result()

        results = []
        for p_idx, passage in enumerate(passages):
            persona_results = {}
            for persona_key in persona_keys:
                conversation = conversations[(p_idx, persona_key)]
                persona_results[persona_key] = {
                    "raw_text": conversation.raw_text,
                    "html": render_html(persona_key, conversation.raw_text),
                    "usage": conversation.usages[-1],
                    "conversation": conversation,
                }
            results.append({**passage, "personas": persona_results})

        return {
            "passages": results,
            "html": render_article_html(article_text, results),
            "selection_raw": selection_raw,
            "selection_usage": selection_usage,
            "model": model,
        }
