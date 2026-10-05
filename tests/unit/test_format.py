from __future__ import annotations

from i3bridge.bridge.ports import ChannelEvent
from i3bridge.discord_ import format as fmt


def test_pinkfish_strip_and_ansi() -> None:
    assert fmt.strip_pinkfish("%^RED%^hi%^RESET%^ there %^BOLD%^%^B_BLUE%^x%^%^") == "hi there x"
    ansi = fmt.pinkfish_to_ansi("%^RED%^hi%^RESET%^")
    assert ansi == "\x1b[31mhi\x1b[0m"
    rendered = fmt.clean_inbound("%^GREEN%^go%^RESET%^", "ansi")
    assert rendered.startswith("```ansi\n") and "\x1b[32mgo" in rendered


def test_clean_inbound_strips_controls_and_escapes_markdown() -> None:
    out = fmt.clean_inbound("\x1b[1mbold\x1b[0m *star* _u_ `t` @everyone\x07\x00 #h")
    assert "\x1b" not in out and "\x07" not in out and "\x00" not in out
    assert out.startswith("bold \\*star\\* \\_u\\_ \\`t\\` @​everyone")
    assert fmt.clean_inbound("# heading\n- item") == "\\# heading\n\\- item"


def test_render_channel_event() -> None:
    msg = ChannelEvent("ig", "Foo", "bob", "Bob", "hello *world*", "message")
    assert fmt.render_channel_event(msg) == "hello \\*world\\*"
    emote = ChannelEvent("ig", "Foo", "bob", "Bob", "$N waves", "emote")
    assert fmt.render_channel_event(emote) == "*Bob@Foo waves*"
    emote2 = ChannelEvent("ig", "Foo", "bob", "Bob", "waves", "emote")
    assert fmt.render_channel_event(emote2) == "*Bob@Foo waves*"
    targeted = ChannelEvent("ig", "Foo", "bob", "Bob", "Bob@Foo pokes Alice@Discord", "targeted")
    assert fmt.render_channel_event(targeted) == "*Bob@Foo pokes Alice@Discord*"
    assert fmt.render_channel_event(ChannelEvent("ig", "Foo", "bob", "Bob", "", "message")) == "​"


def test_webhook_username_rules() -> None:
    assert fmt.webhook_username("Bob", "Foo") == "Bob@Foo"
    assert "discord" not in fmt.webhook_username("Bob", "DiscordMUD").lower()
    assert "clyde" not in fmt.webhook_username("Clyde", "Foo").lower()
    assert len(fmt.webhook_username("x" * 100, "y" * 100)) == 80
    assert fmt.webhook_username("\x00", "") == "I3 user"


def test_ascii_name() -> None:
    assert fmt.ascii_name("Zoë  Ünïcode\n") == "Zoe Unicode"
    assert fmt.ascii_name("日本語") == "Ri Ben Yu"
    assert fmt.ascii_name("\x01") == "someone"
    assert len(fmt.ascii_name("a" * 100)) == 40


def test_italic_emote() -> None:
    assert fmt.italic_emote("_waves_") == "waves"
    assert fmt.italic_emote("*waves at everyone*") == "waves at everyone"
    assert fmt.italic_emote("_a_ and _b_") is None
    assert fmt.italic_emote("plain") is None
    assert fmt.italic_emote("__") is None


def test_discord_to_i3_mentions_markdown_and_splitting() -> None:
    names = {("user", 1): "Alice", ("channel", 2): "general", ("role", 3): "Admins"}
    lines = fmt.discord_to_i3(
        "Hey <@1> and <@!1>, see <#2> <@&3> <:smile:9> <a:wave:10> "
        "**bold** `code` ~~x~~ ||s|| *i* _u_ Zoë",
        resolve=lambda kind, ident: names.get((kind, ident)),
    )
    assert lines == [
        "Hey @Alice and @Alice, see #general @Admins :smile: :wave: bold code x s i u Zoe"
    ]
    joined = fmt.discord_to_i3("line one\n\nline two", resolve=lambda k, i: None)
    assert joined == ["line one / line two"]
    split = fmt.discord_to_i3("line one\nline two", resolve=lambda k, i: None, multiline="split")
    assert split == ["line one", "line two"]
    with_files = fmt.discord_to_i3(
        "look", resolve=lambda k, i: None, attachments=["https://x/y.png"]
    )
    assert with_files == ["look / https://x/y.png"]
    assert fmt.discord_to_i3("   \n", resolve=lambda k, i: None) == []
    long = fmt.discord_to_i3("word " * 300, resolve=lambda k, i: None, max_chars=100)
    assert len(long) > 1 and all(len(line) <= 100 for line in long)
    assert fmt.discord_to_i3("```py\nx = 1\ny = 2\n```", resolve=lambda k, i: None) == [
        "x = 1 / y = 2"
    ]
    assert fmt.split_line("a" * 250, 100) == ["a" * 100, "a" * 100, "a" * 50]


def test_transliteration_modes_keep_emoji_by_default() -> None:
    msg = "Zoë says hi 👋 to 🇺🇸 👍🏽 and ❤️ 日本"
    text = fmt.discord_to_i3(msg, resolve=lambda k, i: None)
    assert text == ["Zoe says hi 👋 to 🇺🇸 👍🏽 and ❤️ Ri Ben"]
    everything = fmt.discord_to_i3(msg, resolve=lambda k, i: None, transliterate="all")
    assert everything == ["Zoe says hi to and Ri Ben"]
    raw = fmt.discord_to_i3(msg, resolve=lambda k, i: None, transliterate="none")
    assert raw == [msg]
    assert fmt.transliterate_text("plain ascii", "text") == "plain ascii"
    assert fmt.transliterate_text("café ☕ 1️⃣", "text") == "cafe ☕ 1️⃣"
