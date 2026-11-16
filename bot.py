"""GAF — Garlic Agentic Framework. Alfred Cockburn and the Joker, at your service."""

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, BufferedInputFile, Message
from aiogram.utils.chat_action import ChatActionSender

import brain
import memory
import prompts
import tools
from prompts import format_line

log = logging.getLogger("gaf")

ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])

DEBOUNCE_S = 2.5  # settle time before an unprompted gate check
COOLDOWN_S = 300  # min gap between unprompted replies
HOURLY_MAX = 4  # unprompted replies per hour
TURN_GAP_S = 8.0  # between bot turns: Telegram allows 20 msg/min per group, shared by both
HISTORY_MAX = 160  # transcript lines kept — big enough that a chain can't evict the humans
HISTORY_TRIM = 20  # dropped at once when full (chunked, keeps the cache prefix stable)
ALFRED_RE = re.compile(
    r"\b(alfred|gaf|cockburn|butler)\b"  # English
    r"|\bаль?фред(?!о\b)\w*",  # Альфред/Алфред + case endings; the (?!о\b) spares пасту альфредо
    re.I,
)
JOKER_RE = re.compile(
    r"\b(joker|jerker|jester)\b"  # English
    r"|\bдж(окер|еркер)\w*",  # Джокер/Джеркер + case endings. No шут: it swallows шутка/шутить.
    re.I,
)
VISION_MIMES = ("image/", "application/pdf", "text/")
VISION_MAX_BYTES = 8 * 1024 * 1024  # Telegram's own download cap is 20 MB

router = Router()


@dataclass
class Persona:
    name: str  # transcript label, /mute key, and {name} in the instructions
    prompt: str
    name_re: re.Pattern
    bot: Bot
    apology: str  # when the brain throws
    blank: str  # when the brain returns nothing
    id: int = 0
    username: str = ""
    unprompted_times: deque[float] = field(default_factory=deque)
    last_unprompted: float = 0.0


PERSONAS = [
    Persona(
        "Alfred",
        prompts.PERSONA,
        ALFRED_RE,
        Bot(os.environ["TELEGRAM_BOT_TOKEN"]),
        "I do beg your pardon — a fault in the plumbing. Do try again.",
        "I find myself, unusually, lost for words.",
    )
]
if os.getenv("JOKER_BOT_TOKEN"):  # absent token = Alfred alone, so local dev and a half-deploy still work
    PERSONAS.append(
        Persona(
            "Joker",
            prompts.JOKER,
            JOKER_RE,
            Bot(os.environ["JOKER_BOT_TOKEN"]),
            "my brain fell out. give me a sec. don't tell Alfred.",
            "...okay that one killed me and I have nothing.",
        )
    )

# Single group, two personas, so state is module-level rather than per-chat.
history: deque[str] = deque()  # shared: this is how the two of them hear each other
turn: asyncio.Task | None = None  # the one live bot activity, ever
reply_lock = asyncio.Lock()
MUTED: set[str] = set()  # lowercase persona names, loaded from SQLite at startup
_seen: deque[int] = deque(maxlen=512)


# Both bots poll, so one human message arrives twice with the same message_id.
# Outer middleware runs before filters, so this covers the commands too.
@router.message.outer_middleware()
async def dedup(handler, event: Message, data: dict):
    if event.message_id in _seen:
        return None
    _seen.append(event.message_id)  # no await between test and append: no interleave
    return await handler(event, data)


def muted(name: str) -> bool:
    return name.lower() in MUTED


def set_mute(name: str, on: bool) -> None:
    # Memory first: a mute must take hold even if the disk write fails.
    MUTED.add(name.lower()) if on else MUTED.discard(name.lower())
    try:
        memory.set_muted(name.lower(), on)
    except Exception:
        log.exception("mute not persisted — it will lapse on restart")


def partner(p: Persona) -> Persona | None:
    """Whose turn is next. None when only one persona is configured."""
    if len(PERSONAS) < 2:
        return None
    return PERSONAS[(PERSONAS.index(p) + 1) % len(PERSONAS)]


def append_history(line: str) -> None:
    history.append(line)
    if len(history) > HISTORY_MAX:
        for _ in range(HISTORY_TRIM):
            history.popleft()


def transcript() -> str:
    return "\n".join(history)


def unprompted_allowed(p: Persona) -> bool:
    now = time.monotonic()
    if now - p.last_unprompted < COOLDOWN_S:
        return False
    while p.unprompted_times and now - p.unprompted_times[0] > 3600:
        p.unprompted_times.popleft()
    return len(p.unprompted_times) < HOURLY_MAX


def is_mention(p: Persona, message: Message, text: str) -> bool:
    if p.username and f"@{p.username.lower()}" in text.lower():
        return True
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == p.id:
        return True
    return bool(p.name_re.search(text))


async def api(call):
    """Every send goes through here: the pair shares Telegram's 20 msg/min per group."""
    try:
        return await call()
    except TelegramRetryAfter as exc:
        log.warning("flood control: waiting %ss", exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        return await call()  # one retry; a second 429 ends the chain, which is the right failure


async def media_note(message: Message, bot: Bot) -> str:
    """A bracketed transcript note for any attachment. Never raises; degrades to a bare label."""
    if message.photo:
        file, mime = message.photo[-1], "image/jpeg"
        plain, tag = "[sent a photo]", "photo"
    elif message.document:
        doc = message.document
        file, mime = doc, doc.mime_type or ""
        tag = doc.file_name or "file"
        plain = f"[sent a file: {tag}]"
    else:
        return ""
    if not mime.startswith(VISION_MIMES) or not 0 < (file.file_size or 0) <= VISION_MAX_BYTES:
        return plain
    try:
        buf = await bot.download(file)  # the bot that received it: file_ids are per-bot
        desc = await tools.describe_media(buf.read(), mime)
    except Exception as exc:
        log.warning("describe failed: %s", exc)  # Gemini is flaky; spare the stack traces
        return plain
    return f"[{tag}: {desc}]" if desc else plain


async def send_chunked(p: Persona, trigger: Message | None, text: str) -> None:
    # 4000, not 4096: Telegram counts UTF-16 code units, so emoji overflow a 4096-char slice.
    for i in range(0, len(text), 4000):
        chunk = text[i : i + 4000]
        if trigger:
            # .as_(p.bot) rebinds: a bare trigger.reply() sends via whichever poller won dedup.
            await api(lambda: trigger.reply(chunk).as_(p.bot))
        else:
            await api(lambda: p.bot.send_message(ALLOWED_CHAT_ID, chunk))


async def deliver(p: Persona, trigger: Message | None, chained: bool = False) -> None:
    """Run one persona's brain and send its reply. trigger is the mention, or None when
    unprompted. chained means a bot prompted this one, which costs him generate_image."""
    async with reply_lock:
        try:
            async with ChatActionSender.typing(bot=p.bot, chat_id=ALLOWED_CHAT_ID):
                text, images = await brain.respond(transcript(), p.prompt, p.name, chained)
        except Exception:
            log.exception("%s: respond failed", p.name)
            if trigger:
                await api(lambda: trigger.reply(p.apology).as_(p.bot))
            return
        if not text and not images:
            text = p.blank
        await send_chunked(p, trigger, text)
        for img in images:
            name = f"{p.name.lower()}.png"
            await api(lambda: p.bot.send_photo(ALLOWED_CHAT_ID, BufferedInputFile(img, filename=name)))
        # Telegram never echoes a bot its own messages, so record them ourselves.
        if text:
            append_history(format_line(p.name, text))
        if images:
            append_history(format_line(p.name, "[sent an image]"))


async def run_chain(p: Persona, trigger: Message | None) -> None:
    """The two of them take turns until one is muted. A loop, never recursion: the chain
    is unbounded by design, and recursive awaits would grow the stack forever."""
    first = True
    while not muted(p.name):
        await deliver(p, trigger, chained=not first)
        trigger, first = None, False  # only the opening turn quotes the human, or draws
        nxt = partner(p)
        if nxt is None:
            return
        p = nxt
        await asyncio.sleep(TURN_GAP_S)


def start(coro) -> None:
    """One live chain, ever. A new human turn supersedes whatever the bots were doing.
    Keeping the handle also stops the task being garbage-collected mid-flight."""
    global turn
    if turn and not turn.done():
        turn.cancel()
    turn = asyncio.create_task(coro)


async def gate_then_chain() -> None:
    try:
        await asyncio.sleep(DEBOUNCE_S)
        for p in sorted(PERSONAS, key=lambda q: q.last_unprompted):  # least recent speaker gets first refusal
            if muted(p.name) or not unprompted_allowed(p):
                continue
            if not await brain.gate(transcript(), p.prompt, p.name):
                continue
            now = time.monotonic()
            p.last_unprompted = now
            p.unprompted_times.append(now)
            await run_chain(p, None)
            return
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("gate failed")


# Registered above on_message: aiogram takes the first matching handler in source order.
# ignore_mention, or /memory@joker falls through to the catch-all and is silently swallowed.
@router.message(Command("memory", ignore_mention=True), F.chat.id == ALLOWED_CHAT_ID)
async def on_memory(message: Message) -> None:
    # Kept out of history: render_for_prompt already puts the ledger in both system prompts.
    await send_chunked(PERSONAS[0], message, memory.render_all() or "The ledger is empty, sir.")


@router.message(Command("mute", "unmute", ignore_mention=True), F.chat.id == ALLOWED_CHAT_ID)
async def on_mute(message: Message, command: CommandObject) -> None:
    on = command.command == "mute"
    arg = (command.args or "all").strip().lower()  # bare /mute is the panic button: everyone
    # name_re so their nicknames work too: /mute jerker, /mute джокер, /mute альфред.
    names = [p.name for p in PERSONAS if arg == "all" or arg == p.name.lower() or p.name_re.search(arg)]
    if not names:
        await send_chunked(PERSONAS[0], message, f"I know no {arg}, sir.")
        return
    for name in names:
        set_mute(name, on)
    if on and turn and not turn.done():
        turn.cancel()  # a mute stops the room; whoever was mid-sentence loses it
    await send_chunked(PERSONAS[0], message, f"{'Silenced' if on else 'Restored'}: {', '.join(names)}")


@router.message(F.chat.id == ALLOWED_CHAT_ID)
async def on_message(message: Message, bot: Bot) -> None:
    user = message.from_user
    if user is None or any(user.id == p.id for p in PERSONAS):
        return  # our own output is recorded in deliver(); never let it round-trip
    caption = message.text or message.caption or ""  # what the human wrote — the only mention trigger
    note = await media_note(message, bot)  # enrichment — never consulted for mentions
    text = f"{note} {caption}".strip()
    if not text:
        return
    append_history(format_line(user.first_name, text))
    if user.is_bot:
        return  # other bots are furniture: seen, never answered
    p = next((q for q in PERSONAS if is_mention(q, message, caption)), None)
    if p:
        start(run_chain(p, message))
    elif any(not muted(q.name) and unprompted_allowed(q) for q in PERSONAS):
        start(gate_then_chain())


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    MUTED.update(memory.muted_names())  # a mute must survive a redeploy
    for p in list(PERSONAS):
        try:
            me = await p.bot.get_me()
        except Exception:
            log.exception("%s could not start; carrying on without him", p.name)
            PERSONAS.remove(p)  # a bad token must not take the whole household down
            continue
        p.id, p.username = me.id, me.username or ""
        await p.bot.delete_webhook(drop_pending_updates=True)  # a redeploy must not replay a backlog
    if not PERSONAS:
        raise SystemExit("no personas could start")
    # One registrant, so the group menu shows plain /memory rather than /memory@each.
    await PERSONAS[0].bot.set_my_commands(
        [
            BotCommand(command="memory", description="what we have committed to memory"),
            BotCommand(command="mute", description="silence us: /mute joker | alfred | all"),
            BotCommand(command="unmute", description="let us speak again"),
        ]
    )
    dp = Dispatcher()
    dp.include_router(router)
    log.info(
        "on duty: %s in chat %s%s",
        ", ".join(f"{p.name} @{p.username}" for p in PERSONAS),
        ALLOWED_CHAT_ID,
        f" (muted: {', '.join(sorted(MUTED))})" if MUTED else "",
    )
    await dp.start_polling(*(p.bot for p in PERSONAS), allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
