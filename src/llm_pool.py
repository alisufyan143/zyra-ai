"""
LLM Connection Pool — Multi-key, multi-model rotation with automatic fallback.

Strategy:
=========
    3 API Keys  ×  3 Models  =  9 fallback slots

    Model Priority (by free-tier RPD limit):
    1. gemini-3.1-flash-lite  (500 RPD, 15 RPM) — workhorse
    2. gemini-3.5-flash       ( 20 RPD,  5 RPM) — newest
    3. gemini-2.5-flash       ( 20 RPD,  5 RPM) — proven

    Key Rotation:
    - Round-robin across keys for each request
    - On 429/rate-limit → skip to next key
    - On all keys exhausted for a model → fall to next model
    - On all models exhausted → wait and retry from top

    Usage Tracking:
    - Counts requests per (key, model) pair
    - Logs which slot served each request
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import instructor
from dotenv import load_dotenv
from google import genai

load_dotenv()
logger = logging.getLogger(__name__)


# ── Model priority order (best free-tier limits first) ──
MODEL_PRIORITY = [
    "gemini-3.1-flash-lite",  # 500 RPD, 15 RPM — highest free-tier quota
    "gemini-3.5-flash",       #  20 RPD,  5 RPM — newest model
    "gemini-2.5-flash",       #  20 RPD,  5 RPM — proven reliable
]

# ── Rate limit config per model ──
MODEL_LIMITS = {
    "gemini-3.1-flash-lite": {"rpm": 15, "rpd": 500, "delay": 5.0},
    "gemini-3.5-flash":      {"rpm": 5,  "rpd": 20,  "delay": 13.0},
    "gemini-2.5-flash":      {"rpm": 5,  "rpd": 20,  "delay": 13.0},
}

# Rate-limit error indicators
RATE_LIMIT_CODES = {429, 503}
RATE_LIMIT_MESSAGES = {"rate limit", "quota", "resource exhausted", "too many requests"}


@dataclass
class SlotStats:
    """Usage stats for a single (key, model) slot."""
    key_index: int
    model: str
    requests: int = 0
    failures: int = 0
    rate_limited: bool = False
    last_request_time: float = 0.0


class LLMPool:
    """
    Manages multiple API keys and models with automatic rotation and fallback.

    Usage:
        pool = LLMPool()
        result = await pool.call(messages=[...], response_model=MyModel)
    """

    def __init__(self):
        self._keys = self._load_keys()
        self._slots: list[SlotStats] = []
        self._clients: dict[int, genai.Client] = {}
        self._current_slot_index = 0
        self._lock = asyncio.Lock()

        # Build slot order: for each model (priority), cycle through keys
        for model in MODEL_PRIORITY:
            for key_idx in range(len(self._keys)):
                self._slots.append(SlotStats(key_index=key_idx, model=model))

        logger.info(
            "LLM Pool initialized: %d keys × %d models = %d slots",
            len(self._keys), len(MODEL_PRIORITY), len(self._slots)
        )
        for i, slot in enumerate(self._slots):
            logger.info("  Slot %d: Key-%d + %s", i, slot.key_index + 1, slot.model)

    def _load_keys(self) -> list[str]:
        """Load all API keys from environment."""
        keys = []
        for i in range(1, 10):  # Support up to 9 keys
            key = os.getenv(f"GEMINI_API_KEY_{i}")
            if key:
                keys.append(key)

        # Fallback: check old single-key format
        if not keys:
            single = os.getenv("GEMINI_API_KEY")
            if single:
                keys.append(single)

        if not keys:
            raise ValueError(
                "No API keys found! Set GEMINI_API_KEY_1, GEMINI_API_KEY_2, etc. in .env"
            )

        logger.info("Loaded %d API keys", len(keys))
        return keys

    def _get_client(self, key_index: int) -> genai.Client:
        """Get or create a genai Client for a specific key."""
        if key_index not in self._clients:
            self._clients[key_index] = genai.Client(api_key=self._keys[key_index])
        return self._clients[key_index]

    def _get_instructor(self, key_index: int):
        """Get an instructor-patched async client for a key."""
        client = self._get_client(key_index)
        return instructor.from_genai(
            client=client,
            mode=instructor.Mode.GENAI_STRUCTURED_OUTPUTS,
            use_async=True,
        )

    async def call(self, messages: list, response_model, max_retries: int = 2) -> Optional[object]:
        """
        Make an LLM call with automatic key/model rotation.

        Tries each slot in order. On rate-limit, skips to next slot.
        On all slots exhausted, waits and retries from the beginning.

        Args:
            messages: Chat messages (instructor format)
            response_model: Pydantic model for structured output
            max_retries: Retries within a single slot (instructor retry for schema fixing)

        Returns:
            Parsed response_model instance, or None on total failure.
        """
        max_full_rotations = 2  # Try the full slot list twice before giving up
        total_slots = len(self._slots)

        for rotation in range(max_full_rotations):
            for offset in range(total_slots):
                async with self._lock:
                    slot_index = (self._current_slot_index + offset) % total_slots
                    slot = self._slots[slot_index]

                    # Skip slots that are rate-limited
                    if slot.rate_limited:
                        continue

                # Enforce per-model rate limiting
                model_config = MODEL_LIMITS.get(slot.model, {"delay": 7.0})
                delay = model_config["delay"]
                now = time.monotonic()
                wait = delay - (now - slot.last_request_time)
                if wait > 0:
                    await asyncio.sleep(wait)

                try:
                    instructor_client = self._get_instructor(slot.key_index)

                    logger.info(
                        "LLM call: Slot %d (Key-%d, %s) | requests=%d",
                        slot_index, slot.key_index + 1, slot.model, slot.requests + 1
                    )

                    result = await instructor_client.create(
                        model=slot.model,
                        messages=messages,
                        response_model=response_model,
                        max_retries=max_retries,
                    )

                    # Success — update stats
                    async with self._lock:
                        slot.requests += 1
                        slot.last_request_time = time.monotonic()
                        # Advance pointer for round-robin within same model
                        self._current_slot_index = (slot_index + 1) % total_slots

                    logger.info(
                        "LLM success: Slot %d (Key-%d, %s)",
                        slot_index, slot.key_index + 1, slot.model
                    )
                    return result

                except Exception as e:
                    error_str = str(e).lower()
                    is_rate_limit = (
                        any(msg in error_str for msg in RATE_LIMIT_MESSAGES)
                        or "429" in error_str
                        or "resource_exhausted" in error_str
                    )

                    if is_rate_limit:
                        logger.warning(
                            "Rate limited: Slot %d (Key-%d, %s) — rotating to next",
                            slot_index, slot.key_index + 1, slot.model
                        )
                        async with self._lock:
                            slot.rate_limited = True
                            slot.failures += 1
                    else:
                        logger.error(
                            "LLM error: Slot %d (Key-%d, %s): %s",
                            slot_index, slot.key_index + 1, slot.model, e
                        )
                        async with self._lock:
                            slot.failures += 1
                        # Non-rate-limit errors: try next slot anyway
                        continue

            # All slots exhausted in this rotation — wait and un-mark rate limits
            if rotation < max_full_rotations - 1:
                logger.warning(
                    "All %d slots exhausted. Waiting 60s before retry (rotation %d/%d)...",
                    total_slots, rotation + 1, max_full_rotations
                )
                await asyncio.sleep(60)
                # Reset rate-limit flags for retry
                async with self._lock:
                    for slot in self._slots:
                        slot.rate_limited = False

        logger.error("All LLM slots failed after %d full rotations. Giving up.", max_full_rotations)
        return None

    def get_stats(self) -> str:
        """Return a formatted string of pool usage stats."""
        lines = ["LLM Pool Usage Stats:"]
        lines.append(f"  {'Slot':<5} {'Key':<6} {'Model':<25} {'Reqs':<6} {'Fails':<6} {'Limited'}")
        lines.append(f"  {'-'*5} {'-'*6} {'-'*25} {'-'*6} {'-'*6} {'-'*7}")
        for i, s in enumerate(self._slots):
            lines.append(
                f"  {i:<5} Key-{s.key_index+1:<2} {s.model:<25} {s.requests:<6} {s.failures:<6} {'YES' if s.rate_limited else ''}"
            )
        total_reqs = sum(s.requests for s in self._slots)
        total_fails = sum(s.failures for s in self._slots)
        lines.append(f"  Total: {total_reqs} requests, {total_fails} failures")
        return "\n".join(lines)
