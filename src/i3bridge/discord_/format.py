"""Text transforms between I3 and Discord. Pure functions; no Discord objects involved."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence

from unidecode import unidecode

from ..bridge.ports import ChannelEvent

# Pinkfish colour tokens as used by LPC mudlibs: %^RED%^, %^BOLD%^, %^RESET%^, %^B_BLUE%^ ...
PINKFISH_RE = re.compile(r"%\^[A-Za-z0-9_]*%\^")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
MENTION_RE = re.compile(r"<(@[!&]?|#|a?:[A-Za-z0-9_~]+:)(\d+)>")
MARKDOWN_PAIRS = [
    (re.compile(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", re.S), r"\1"),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),
    (re.compile(r"\|\|(.+?)\|\|", re.S), r"\1"),
    (re.compile(r"(?<![A-Za-z0-9])\*([^*\n]+)\*(?![A-Za-z0-9])"), r"\1"),
    (re.compile(r"(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])"), r"\1"),
    (re.compile(r"^#{1,3} ", re.M), ""),
    (re.compile(r"^> ?", re.M), ""),
]
ITALIC_RE = re.compile(r"^(?:_([^_]+)_|\*([^*]+)\*)$", re.S)
MD_ESCAPE_RE = re.compile(r"([\\*_~`|>])")
MD_LINE_START_RE = re.compile(r"^([#\-+] |\d+\. )", re.M)

PINKFISH_ANSI = {
    "RESET": "0", "BOLD": "1", "UNDERLINE": "4", "FLASH": "5", "REVERSE": "7",
    "BLACK": "30", "RED": "31", "GREEN": "32", "YELLOW": "33", "ORANGE": "33",
    "BLUE": "34", "MAGENTA": "35", "CYAN": "36", "WHITE": "37",
    "B_BLACK": "40", "B_RED": "41", "B_GREEN": "42", "B_YELLOW": "43",
    "B_BLUE": "44", "B_MAGENTA": "45", "B_CYAN": "46", "B_WHITE": "47",
}  # fmt: skip

DISCORD_MAX = 2000
WEBHOOK_NAME_MAX = 80
FORBIDDEN_WEBHOOK_WORDS = {"discord": "disc0rd", "clyde": "clyd3"}


def strip_pinkfish(text: str) -> str:
    return PINKFISH_RE.sub("", text)


def pinkfish_to_ansi(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        token = m.group(0)[2:-2].upper()
        code = PINKFISH_ANSI.get(token)
        return f"\x1b[{code}m" if code else ""

    return PINKFISH_RE.sub(repl, text)


def clean_inbound(text: str, color_mode: str = "strip") -> str:
    """Make I3 text safe and readable for Discord. Returns Discord-markup-escaped text."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if color_mode == "ansi" and PINKFISH_RE.search(text):
        text = pinkfish_to_ansi(ANSI_RE.sub("", text))
        text = CONTROL_RE.sub("", text.replace("\x1b", "\ue000")).replace("\ue000", "\x1b")
        body = "\n".join(line.rstrip() for line in text.split("\n")).strip()
        return f"```ansi\n{body[: DISCORD_MAX - 12]}\n```"
    text = ANSI_RE.sub("", strip_pinkfish(text))
    text = CONTROL_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return escape_markdown(text)[:DISCORD_MAX]


def escape_markdown(text: str) -> str:
    text = MD_ESCAPE_RE.sub(r"\\\1", text)
    text = MD_LINE_START_RE.sub(r"\\\1", text)
    return text.replace("@everyone", "@​everyone").replace("@here", "@​here")


def render_channel_event(ev: ChannelEvent, color_mode: str = "strip") -> str:
    actor = f"{ev.visname}@{ev.mud}"
    if ev.kind == "message":
        return clean_inbound(ev.text, color_mode) or "​"
    if ev.kind == "emote":
        text = ev.text if "$N" in ev.text else f"$N {ev.text}"
        text = text.replace("$N", actor)
    else:
        text = ev.text
    cleaned = clean_inbound(text, color_mode)
    return cleaned if cleaned.startswith("```") else f"*{cleaned}*"


def webhook_username(visname: str, mud: str) -> str:
    visname = CONTROL_RE.sub("", visname).strip()
    mud = CONTROL_RE.sub("", mud).strip()
    name = f"{visname}@{mud}" if visname and mud else (visname or mud or "I3 user")
    low = name.lower()
    for bad, good in FORBIDDEN_WEBHOOK_WORDS.items():
        if bad in low:
            idx = low.index(bad)
            name = name[:idx] + good + name[idx + len(bad) :]
            low = name.lower()
    if low in ("everyone", "here"):
        name = f"{name}_"
    return name[:WEBHOOK_NAME_MAX]


TRANSLITERATE_MODES = ("none", "text", "all")
_EMOJI_JOINERS = frozenset("\u200d\ufe0f\ufe0e\u20e3")


def _is_pictographic(ch: str) -> bool:
    if ch in _EMOJI_JOINERS:
        return True
    code = ord(ch)
    if 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or 0x1F1E6 <= code <= 0x1F1FF:
        return True
    return unicodedata.category(ch) in ("So", "Sk")


def transliterate_text(text: str, mode: str) -> str:
    """``none``: unchanged. ``all``: everything through unidecode (emoji are lost).
    ``text``: letters and punctuation through unidecode, emoji and symbols kept as UTF-8."""
    if mode == "none":
        return text
    if mode == "all":
        return str(unidecode(text))
    out: list[str] = []
    run: list[str] = []
    for ch in text:
        if ch.isascii():
            run.append(ch)
        elif _is_pictographic(ch):
            if run:
                out.append(str(unidecode("".join(run))))
                run = []
            out.append(ch)
        else:
            run.append(ch)
    if run:
        out.append(str(unidecode("".join(run))))
    return "".join(out)


def ascii_name(text: str, *, max_len: int = 40, fallback: str = "someone") -> str:
    """ASCII-only, printable, single-line form of a name for the I3 side."""
    out = CONTROL_RE.sub("", unidecode(text)).replace("\n", " ").strip()
    out = re.sub(r"\s+", " ", out)
    return (out or fallback)[:max_len]


def italic_emote(content: str) -> str | None:
    """If the whole message is italic (Discord's built-in /me), return the inner text."""
    m = ITALIC_RE.match(content.strip())
    if not m:
        return None
    inner = (m.group(1) or m.group(2) or "").strip()
    return inner or None


def discord_to_i3(
    content: str,
    *,
    resolve: Callable[[str, int], str | None],
    attachments: Sequence[str] = (),
    transliterate: str = "text",
    max_chars: int = 900,
    multiline: str = "join",
) -> list[str]:
    """Turn a Discord message into one or more plain I3 lines."""

    def mention(m: re.Match[str]) -> str:
        kind, ident = m.group(1), int(m.group(2))
        if kind.startswith(":") or kind.startswith("a:"):
            return f":{kind.split(':')[1]}:"
        if kind == "#":
            return "#" + (resolve("channel", ident) or "channel")
        if kind == "@&":
            return "@" + (resolve("role", ident) or "role")
        return "@" + (resolve("user", ident) or "someone")

    text = MENTION_RE.sub(mention, content)
    for pattern, repl in MARKDOWN_PAIRS:
        text = pattern.sub(repl, text)
    text = text.replace("\\", "")
    text = CONTROL_RE.sub("", text.replace("\r", "")).replace("\t", " ")
    text = transliterate_text(text, transliterate)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    if attachments:
        lines.append(" ".join(attachments))
    if not lines:
        return []
    if multiline == "join" and len(lines) > 1:
        lines = [" / ".join(lines)]
    out: list[str] = []
    for line in lines:
        out.extend(split_line(line, max_chars))
    return out


def split_line(line: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(line) <= max_chars:
        return [line]
    parts: list[str] = []
    while len(line) > max_chars:
        cut = line.rfind(" ", max_chars // 2, max_chars)
        if cut == -1:
            cut = max_chars
        parts.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    if line:
        parts.append(line)
    return parts
