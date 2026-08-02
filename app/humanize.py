"""Make the bot's messages feel like a real person texting.

Three tricks do most of the work:
  1. strip markdown / formatting  → real people don't send **bold** or bullets
  2. split into a few short bubbles → real people send several short texts
  3. show 'typing…' with a delay proportional to length → not an instant essay
"""
from __future__ import annotations

import asyncio
import random
import re

from telegram.constants import ChatAction

# Timing bounds (seconds).
_THINK_MIN, _THINK_MAX = 0.4, 1.2      # pause before the first reply
_BUBBLE_MIN, _BUBBLE_MAX = 0.7, 4.5    # typing time per bubble


def strip_markdown(text: str) -> str:
    """Remove formatting an LLM might emit, leaving plain chat text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)     # **bold**
    text = re.sub(r"__(.+?)__", r"\1", text)         # __bold__
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)  # *italic*
    text = re.sub(r"`{1,3}([^`]+)`{1,3}", r"\1", text)     # `code`
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)   # # headings
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)       # - bullets
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.M)       # 1. lists
    return text.strip()


def split_bubbles(text: str, max_bubbles: int) -> list[str]:
    """Break a reply into a few natural chat bubbles."""
    text = text.strip()
    if not text:
        return []

    # Prefer the model's own paragraph breaks.
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    # If it came back as one blob, split long ones by sentence.
    if len(parts) <= 1:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if len(sentences) <= 1:
            parts = sentences or [text]
        else:
            group = 1 if len(sentences) > 3 else 2  # 1–2 sentences per bubble
            parts = [
                " ".join(sentences[i:i + group])
                for i in range(0, len(sentences), group)
            ]

    # Collapse stray newlines inside a bubble.
    parts = [re.sub(r"\s*\n\s*", " ", p).strip() for p in parts if p.strip()]

    # Cap the number of bubbles; fold the rest into the last one.
    if max_bubbles > 0 and len(parts) > max_bubbles:
        head = parts[: max_bubbles - 1]
        head.append(" ".join(parts[max_bubbles - 1:]))
        parts = head
    return parts


def typing_delay(text: str, cps: float) -> float:
    """How long a human would take to type this bubble."""
    secs = len(text) / max(6.0, cps)
    return max(_BUBBLE_MIN, min(_BUBBLE_MAX, secs))


async def deliver(bot, chat_id: int, text: str, cfg) -> None:
    """Send `text` to the chat the way a person would — or plainly if disabled."""
    text = strip_markdown(text)
    if not text:
        return
    if not getattr(cfg, "humanize", True):
        await bot.send_message(chat_id, text)
        return

    bubbles = split_bubbles(text, cfg.max_bubbles)
    await asyncio.sleep(random.uniform(_THINK_MIN, _THINK_MAX))
    for bubble in bubbles:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:  # noqa: BLE001 - typing indicator is best-effort
            pass
        delay = typing_delay(bubble, cfg.typing_cps) * random.uniform(0.85, 1.15)
        await asyncio.sleep(delay)
        await bot.send_message(chat_id, bubble)
