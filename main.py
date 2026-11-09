"""
Authorized Telegram media archival/delivery bot.

This bot keeps two Telethon clients alive in one asyncio program:

1. user_client
   A user session that can read messages/media from chats where that user is
   already authorized.

2. bot_client
   A bot-token session that receives Telegram message links and sends the
   requested content back to the requesting chat.

Important operating model
-------------------------
Use this only for chats and content you own or have explicit permission to
process. By default, this script requires an ALLOWED_CHAT_IDS allowlist unless
ALLOW_ANY_CHAT=true is explicitly set.

Environment variables
---------------------
Required:
  API_ID=123456
  API_HASH=0123456789abcdef0123456789abcdef
  BOT_TOKEN=123456:ABCDEF...

User session, choose one:
  USER_SESSION=...                 # Telethon StringSession
  USER_SESSION_FILE=user_session   # Local .session filename/path

Recommended:
  OWNER_IDS=11111111,22222222      # Users allowed to command the bot
  ALLOWED_CHAT_IDS=-1001234567890,@my_public_channel,1234567890

Optional:
  ALLOW_ANY_CHAT=true             # Set true only for trusted private use
  CONCURRENT_IO=3                  # Use 3 or 5 for bounded throughput
  TMP_DOWNLOAD_DIR=tmp_media
  BOT_SESSION_FILE=bot_session
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import random
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional, Sequence

import aiofiles.os as aio_os
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message


LOG = logging.getLogger("restricted_content_archiver")


# Matches:
#   https://t.me/some_public_channel/123
#   https://telegram.me/some_public_channel/123?single
#   https://t.me/c/1234567890/123
#
# Telegram private channel links omit the MTProto marker. For
# t.me/c/1234567890/42, the marked peer id is -1001234567890.
LINK_RE = re.compile(
    r"(?ix)"
    r"""
    (?:https?://)?
    (?:www\.)?
    t(?:elegram)?\.me/
    (?:
        c/(?P<private_chat_id>\d+)/(?P<private_message_id>\d+)
        |
        (?:s/)?(?P<username>[a-zA-Z0-9_]{5,32})/(?P<public_message_id>\d+)
    )
    (?:[/?#][^\s]*)?
    """
)


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    user_session: Optional[str]
    user_session_file: str
    bot_session_file: str
    owner_ids: set[int]
    allowed_chat_ids: set[str]
    allow_any_chat: bool
    concurrent_io: int
    tmp_download_dir: Path


@dataclass(frozen=True)
class ParsedLink:
    original: str
    entity_ref: str | int
    message_id: int
    kind: str
    private_link_chat_id: Optional[int] = None


@dataclass
class UploadedMedia:
    index: int
    message: Message
    local_path: Path
    uploaded_file: object


def _bool_from_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_set(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _csv_int_set(name: str) -> set[int]:
    values: set[int] = set()
    for item in _csv_set(name):
        try:
            values.add(int(item))
        except ValueError:
            raise ValueError(f"{name} contains a non-integer value: {item!r}")
    return values


def load_settings() -> Settings:
    """Read environment variables once at startup and validate hard failures."""

    missing = [name for name in ("API_ID", "API_HASH", "BOT_TOKEN") if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

    try:
        api_id = int(os.environ["API_ID"])
    except ValueError as exc:
        raise RuntimeError("API_ID must be an integer") from exc

    # The semaphore is the hard cap for concurrent disk/network I/O workers.
    # Values above 5 usually buy little and can increase FloodWait pressure.
    concurrent_io = int(os.getenv("CONCURRENT_IO", "3"))
    if concurrent_io < 1:
        raise RuntimeError("CONCURRENT_IO must be at least 1")
    if concurrent_io > 5:
        LOG.warning("CONCURRENT_IO=%s is high; clamping to 5", concurrent_io)
        concurrent_io = 5

    return Settings(
        api_id=api_id,
        api_hash=os.environ["API_HASH"],
        bot_token=os.environ["BOT_TOKEN"],
        user_session=os.getenv("USER_SESSION") or None,
        user_session_file=os.getenv("USER_SESSION_FILE", "user_session"),
        bot_session_file=os.getenv("BOT_SESSION_FILE", "bot_session"),
        owner_ids=_csv_int_set("OWNER_IDS"),
        allowed_chat_ids={item.lower() for item in _csv_set("ALLOWED_CHAT_IDS")},
        allow_any_chat=_bool_from_env("ALLOW_ANY_CHAT", default=False),
        concurrent_io=concurrent_io,
        tmp_download_dir=Path(os.getenv("TMP_DOWNLOAD_DIR", "tmp_media")),
    )


def parse_message_link(text: str) -> Optional[ParsedLink]:
    """Extract the first supported Telegram message link from free text."""

    match = LINK_RE.search(text or "")
    if not match:
        return None

    if match.group("private_chat_id"):
        private_chat_id = int(match.group("private_chat_id"))
        message_id = int(match.group("private_message_id"))

        # MTProto "marked" channel ids use the -100 prefix. Telethon accepts
        # this integer form and resolves it to the corresponding InputPeer.
        mtproto_peer_id = int(f"-100{private_chat_id}")

        return ParsedLink(
            original=match.group(0),
            entity_ref=mtproto_peer_id,
            message_id=message_id,
            kind="private",
            private_link_chat_id=private_chat_id,
        )

    username = match.group("username")
    return ParsedLink(
        original=match.group(0),
        entity_ref=username,
        message_id=int(match.group("public_message_id")),
        kind="public",
    )


def _normal_allowed_tokens(settings: Settings) -> set[str]:
    """Normalize allowlist tokens for low-friction config."""

    tokens: set[str] = set()
    for item in settings.allowed_chat_ids:
        if item.startswith("@"):
            tokens.add(item.lower())
        else:
            tokens.add(item)
    return tokens


def is_chat_allowed(entity: object, parsed: ParsedLink, settings: Settings) -> bool:
    """Allow by marked peer id, private-link id, or public @username."""

    if settings.allow_any_chat:
        return True

    allowed = _normal_allowed_tokens(settings)
    if not allowed:
        return False

    # Marked peer id examples:
    #   channel: -1001234567890
    #   basic chat: -123456789
    peer_id = utils.get_peer_id(entity)
    candidates = {str(peer_id)}

    # For t.me/c links, operators often copy the bare c/<id> part into config.
    if parsed.private_link_chat_id is not None:
        candidates.add(str(parsed.private_link_chat_id))
        candidates.add(f"-100{parsed.private_link_chat_id}")

    username = getattr(entity, "username", None)
    if username:
        candidates.add(username.lower())
        candidates.add(f"@{username.lower()}")

    return bool(candidates & allowed)


def _first_text_message(messages: Sequence[Message]) -> Message:
    """Prefer the album item that actually carries the caption."""

    for message in messages:
        if message.raw_text:
            return message
    return messages[0]


async def with_flood_retry(
    operation: Callable[[], Awaitable[object]],
    label: str,
    *,
    max_attempts: int = 6,
) -> object:
    """
    Run a Telethon coroutine factory with FloodWait-aware retry.

    The operation is a factory rather than an already-created coroutine because
    a failed coroutine cannot be awaited twice.
    """

    attempt = 0
    while True:
        try:
            return await operation()
        except FloodWaitError as exc:
            attempt += 1
            if attempt >= max_attempts:
                raise

            # Add a tiny jitter so a batch of concurrent workers does not wake
            # up on the exact same loop tick and immediately collide again.
            wait_for = int(exc.seconds) + 1 + random.uniform(0.0, 1.0)
            LOG.warning(
                "FloodWait during %s: sleeping %.1fs before retry %s/%s",
                label,
                wait_for,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(wait_for)


async def safe_remove(path: Path) -> None:
    """Best-effort async file deletion; logs but does not mask delivery errors."""

    try:
        await aio_os.remove(path)
        LOG.debug("Removed temp file %s", path)
    except FileNotFoundError:
        return
    except OSError:
        LOG.exception("Could not remove temp file %s", path)


async def cleanup_uploads(uploaded: Iterable[UploadedMedia]) -> None:
    """Remove all local files produced for a request."""

    await asyncio.gather(*(safe_remove(item.local_path) for item in uploaded), return_exceptions=True)


def _source_filename(message: Message) -> Optional[str]:
    """Read Telegram's original filename attribute when it exists."""

    document = message.document
    if not document:
        return None

    for attr in document.attributes or []:
        if isinstance(attr, types.DocumentAttributeFilename):
            return attr.file_name
    return None


def _extension_for_message(message: Message) -> str:
    """Choose a stable extension so Telethon writes to the exact path we pass."""

    filename = _source_filename(message)
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix

    if message.photo:
        return ".jpg"

    if message.document and message.document.mime_type:
        guessed = mimetypes.guess_extension(message.document.mime_type)
        if guessed:
            return guessed

    return ".bin"


def temp_path_for(message: Message, index: int, directory: Path) -> Path:
    """Create a collision-resistant path for one downloaded media item."""

    extension = _extension_for_message(message)
    return directory / f"{message.id}_{index}_{uuid.uuid4().hex}{extension}"


def _document_attributes(message: Message, local_path: Path) -> list[types.TypeDocumentAttribute]:
    """
    Preserve Telegram document attributes where possible.

    Attributes carry important presentation details such as original filename,
    video dimensions/duration, audio performer/title, voice note flag, and
    animated GIF metadata.
    """

    attrs: list[types.TypeDocumentAttribute] = []
    if message.document:
        attrs.extend(message.document.attributes or [])

    has_filename = any(isinstance(attr, types.DocumentAttributeFilename) for attr in attrs)
    if not has_filename:
        attrs.append(types.DocumentAttributeFilename(file_name=local_path.name))

    return attrs


def build_uploaded_input_media(uploaded: UploadedMedia) -> types.TypeInputMedia:
    """
    Convert an already-uploaded file handle into raw MTProto input media.

    Telethon's high-level send_file is excellent for simple sends, but building
    InputSingleMedia explicitly gives us precise per-item captions/entities for
    albums while still using the bot client's async upload_file step.
    """

    message = uploaded.message

    if message.photo:
        return types.InputMediaUploadedPhoto(file=uploaded.uploaded_file)

    mime_type = "application/octet-stream"
    if message.document and message.document.mime_type:
        mime_type = message.document.mime_type
    else:
        guessed, _ = mimetypes.guess_type(uploaded.local_path.name)
        if guessed:
            mime_type = guessed

    return types.InputMediaUploadedDocument(
        file=uploaded.uploaded_file,
        mime_type=mime_type,
        attributes=_document_attributes(message, uploaded.local_path),
    )


def _random_id() -> int:
    """Telegram requires a unique 64-bit random_id for each outgoing media."""

    return random.getrandbits(63)


async def prepare_upload(
    *,
    index: int,
    message: Message,
    user_client: TelegramClient,
    bot_client: TelegramClient,
    semaphore: asyncio.Semaphore,
    download_dir: Path,
) -> UploadedMedia:
    """
    Download one message's media with the user session, then upload it with the bot.

    The semaphore covers the whole disk/network I/O section, which is the part
    that can overload local storage, Telegram CDN sessions, or bot API limits.
    Album callers intentionally spawn many of these with create_task(); the
    semaphore is the hard throughput governor.
    """

    local_path = temp_path_for(message, index, download_dir)

    async with semaphore:
        try:
            downloaded_path = await with_flood_retry(
                lambda: user_client.download_media(message, file=str(local_path)),
                f"download message {message.id}",
            )
            if not downloaded_path:
                raise RuntimeError(f"Message {message.id} did not produce a downloadable file")

            local_path = Path(downloaded_path)

            uploaded_file = await with_flood_retry(
                lambda: bot_client.upload_file(str(local_path)),
                f"upload message {message.id}",
            )

            return UploadedMedia(
                index=index,
                message=message,
                local_path=local_path,
                uploaded_file=uploaded_file,
            )
        except BaseException:
            # If a worker fails or is cancelled after creating a file, keep the
            # zero-accumulation guarantee for partial request state.
            await safe_remove(local_path)
            raise


async def fetch_target_message(
    user_client: TelegramClient,
    entity: object,
    message_id: int,
) -> Message:
    message = await with_flood_retry(
        lambda: user_client.get_messages(entity, ids=message_id),
        f"fetch message {message_id}",
    )

    if message is None:
        raise LookupError(f"Message {message_id} was not found")
    return message


async def collect_album_or_single(
    user_client: TelegramClient,
    entity: object,
    target: Message,
    *,
    lookaround: int = 10,
) -> list[Message]:
    """
    Reconstruct a Telegram album by scanning target.id +/- lookaround.

    get_messages(ids=[...]) is efficient because it fetches an exact sparse set
    rather than walking the whole chat history. Filtering by grouped_id avoids
    accidentally mixing nearby albums.
    """

    if not target.grouped_id:
        return [target]

    start = max(1, target.id - lookaround)
    ids = list(range(start, target.id + lookaround + 1))

    nearby = await with_flood_retry(
        lambda: user_client.get_messages(entity, ids=ids),
        f"fetch album around message {target.id}",
    )

    album = [
        message
        for message in nearby
        if message is not None and message.grouped_id == target.grouped_id and message.media
    ]
    album.sort(key=lambda item: item.id)

    if not album:
        raise LookupError(f"Could not reconstruct album for grouped_id={target.grouped_id}")
    return album


async def send_text_only(
    bot_client: TelegramClient,
    destination: object,
    message: Message,
) -> None:
    """Forward a text-only message by copying raw text and Telegram entities."""

    text = message.raw_text or ""
    if not text:
        text = "[empty message]"

    await with_flood_retry(
        lambda: bot_client.send_message(
            destination,
            text,
            formatting_entities=message.entities or None,
        ),
        f"send text message {message.id}",
    )


async def send_single_uploaded(
    bot_client: TelegramClient,
    destination: object,
    uploaded: UploadedMedia,
    caption_source: Message,
) -> None:
    """Send one uploaded media item with its original caption entities."""

    peer = await bot_client.get_input_entity(destination)
    input_media = build_uploaded_input_media(uploaded)

    await with_flood_retry(
        lambda: bot_client(
            functions.messages.SendMediaRequest(
                peer=peer,
                media=input_media,
                message=caption_source.raw_text or "",
                random_id=_random_id(),
                entities=caption_source.entities or [],
            )
        ),
        f"send single media {uploaded.message.id}",
    )


async def send_album_uploaded(
    bot_client: TelegramClient,
    destination: object,
    uploaded_items: Sequence[UploadedMedia],
    caption_source: Message,
) -> None:
    """
    Send uploaded media as coherent Telegram albums.

    Telegram albums have a hard server-side limit of 10 media items. Albums are
    normally <=10, but chunking keeps the sender safe if Telegram ever returns a
    larger grouped_id span or a link points into an unusual imported history.
    """

    peer = await bot_client.get_input_entity(destination)
    ordered = sorted(uploaded_items, key=lambda item: item.index)

    for chunk_start in range(0, len(ordered), 10):
        chunk = ordered[chunk_start : chunk_start + 10]
        multi_media: list[types.InputSingleMedia] = []

        for offset, uploaded in enumerate(chunk):
            is_first_item_of_first_chunk = chunk_start == 0 and offset == 0
            caption = caption_source.raw_text if is_first_item_of_first_chunk else ""
            entities = caption_source.entities if is_first_item_of_first_chunk else []

            multi_media.append(
                types.InputSingleMedia(
                    media=build_uploaded_input_media(uploaded),
                    message=caption or "",
                    random_id=_random_id(),
                    entities=entities or [],
                )
            )

        await with_flood_retry(
            lambda: bot_client(
                functions.messages.SendMultiMediaRequest(
                    peer=peer,
                    multi_media=multi_media,
                )
            ),
            f"send album chunk starting {chunk_start}",
        )


async def upload_album_concurrently(
    *,
    messages: Sequence[Message],
    user_client: TelegramClient,
    bot_client: TelegramClient,
    semaphore: asyncio.Semaphore,
    download_dir: Path,
) -> list[UploadedMedia]:
    """
    Spawn one worker task per media item and gather the uploaded file handles.

    This is the performance-critical section: downloads and uploads overlap up
    to CONCURRENT_IO while preserving original album order by index.
    """

    tasks = [
        asyncio.create_task(
            prepare_upload(
                index=index,
                message=message,
                user_client=user_client,
                bot_client=bot_client,
                semaphore=semaphore,
                download_dir=download_dir,
            ),
            name=f"download-upload-{message.id}",
        )
        for index, message in enumerate(messages)
    ]

    try:
        uploaded = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Successful workers already returned local file paths; remove them
        # because the request will not be delivered as a complete album.
        await cleanup_uploads(item for item in results if isinstance(item, UploadedMedia))
        raise

    return sorted(uploaded, key=lambda item: item.index)


async def process_link_request(
    *,
    event: events.NewMessage.Event,
    parsed: ParsedLink,
    settings: Settings,
    user_client: TelegramClient,
    bot_client: TelegramClient,
    semaphore: asyncio.Semaphore,
) -> None:
    """Resolve, fetch, upload, send, and clean up one user request."""

    status = await event.reply("Resolving link...")
    destination = event.chat_id
    uploaded_items: list[UploadedMedia] = []

    try:
        entity = await with_flood_retry(
            lambda: user_client.get_entity(parsed.entity_ref),
            f"resolve entity {parsed.entity_ref}",
        )

        if not is_chat_allowed(entity, parsed, settings):
            await status.edit(
                "This chat is not in ALLOWED_CHAT_IDS. "
                "Add it to the allowlist, or set ALLOW_ANY_CHAT=true for trusted private deployments."
            )
            return

        await status.edit("Fetching message...")
        target = await fetch_target_message(user_client, entity, parsed.message_id)

        messages = await collect_album_or_single(user_client, entity, target)
        media_messages = [message for message in messages if message.media]
        caption_source = _first_text_message(messages)

        if not media_messages:
            await status.edit("Sending text...")
            await send_text_only(bot_client, destination, caption_source)
            await status.edit("Done.")
            return

        if len(media_messages) == 1:
            await status.edit("Downloading and uploading media...")
            uploaded = await prepare_upload(
                index=0,
                message=media_messages[0],
                user_client=user_client,
                bot_client=bot_client,
                semaphore=semaphore,
                download_dir=settings.tmp_download_dir,
            )
            uploaded_items = [uploaded]

            await status.edit("Sending media...")
            await send_single_uploaded(bot_client, destination, uploaded, caption_source)
        else:
            await status.edit(f"Uploading album with {len(media_messages)} items...")
            uploaded_items = await upload_album_concurrently(
                messages=media_messages,
                user_client=user_client,
                bot_client=bot_client,
                semaphore=semaphore,
                download_dir=settings.tmp_download_dir,
            )

            await status.edit("Sending album...")
            await send_album_uploaded(bot_client, destination, uploaded_items, caption_source)

        await status.edit("Done.")
    except FloodWaitError as exc:
        LOG.exception("FloodWait exhausted while processing %s", parsed.original)
        await status.edit(f"Telegram rate-limited this request for {exc.seconds}s. Try again later.")
    except LookupError as exc:
        await status.edit(str(exc))
    except RPCError as exc:
        LOG.exception("Telegram RPC error while processing %s", parsed.original)
        await status.edit(f"Telegram rejected the request: {exc.__class__.__name__}")
    except Exception:
        LOG.exception("Unexpected error while processing %s", parsed.original)
        await status.edit("Unexpected error while processing that link. Check server logs.")
    finally:
        # Cleanup happens after delivery attempt. On success this removes every
        # local file immediately; on failure it prevents partial accumulation.
        await cleanup_uploads(uploaded_items)


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def main() -> None:
    configure_logging()
    settings = load_settings()
    settings.tmp_download_dir.mkdir(parents=True, exist_ok=True)

    # Use StringSession in production if available; otherwise Telethon will
    # create/read a local user_session.session file and may prompt on first run.
    user_session = (
        StringSession(settings.user_session)
        if settings.user_session
        else settings.user_session_file
    )

    user_client = TelegramClient(user_session, settings.api_id, settings.api_hash)
    bot_client = TelegramClient(settings.bot_session_file, settings.api_id, settings.api_hash)

    io_semaphore = asyncio.Semaphore(settings.concurrent_io)

    @bot_client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        if not event.raw_text:
            return

        sender_id = event.sender_id
        if settings.owner_ids and sender_id not in settings.owner_ids:
            await event.reply("You are not authorized to use this bot.")
            return

        parsed = parse_message_link(event.raw_text)
        if not parsed:
            await event.reply("Send a Telegram message link, for example: https://t.me/channel/123")
            return

        await process_link_request(
            event=event,
            parsed=parsed,
            settings=settings,
            user_client=user_client,
            bot_client=bot_client,
            semaphore=io_semaphore,
        )

    try:
        await user_client.start()
        await bot_client.start(bot_token=settings.bot_token)

        me = await bot_client.get_me()
        LOG.info("Bot client started as @%s", getattr(me, "username", None) or me.id)
        LOG.info("Concurrent media workers: %s", settings.concurrent_io)

        if not settings.owner_ids:
            LOG.warning("OWNER_IDS is empty; any user who can message the bot may send requests")
        if not settings.allowed_chat_ids and not settings.allow_any_chat:
            LOG.warning("ALLOWED_CHAT_IDS is empty and ALLOW_ANY_CHAT=false; all content links will be denied")

        await bot_client.run_until_disconnected()
    finally:
        await asyncio.gather(
            user_client.disconnect(),
            bot_client.disconnect(),
            return_exceptions=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
