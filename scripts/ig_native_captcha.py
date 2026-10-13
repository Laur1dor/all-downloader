"""Instagram's native text CAPTCHA and Facebook's first-party audio player."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

_DIGITS = dict(
    zip(
        (
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
        ),
        "0123456789",
        strict=True,
    )
)


def present(page) -> bool:
    if urlsplit(page.url).hostname != "www.instagram.com":
        return False
    hear = page.get_by_text("Hear this code", exact=True)
    return bool(hear.count() and hear.first.is_visible())


def normalise_answer(text: str) -> str | None:
    # Accept only clear, short letters/digits or individually spoken numbers.
    # Do not guess when ASR returned instructions or uncertain prose.
    words = re.findall(r"[a-z0-9]+", text.lower())
    answer = "".join(_DIGITS.get(word, word) for word in words)
    if words and all(
        word in _DIGITS or len(word) == 1 or word.isdigit() for word in words
    ):
        if 4 <= len(answer) <= 8:
            return answer
    if len(words) == 1 and 4 <= len(answer) <= 8 and any(c.isdigit() for c in answer):
        return answer
    return None


def solve(page, log, deadline: float) -> bool:
    from igcaptcha import MAX_ATTEMPTS, _audio, _transcribe, trusted_audio

    for attempt in range(MAX_ATTEMPTS):
        if deadline - time.monotonic() < 5:
            break
        captured = []

        def capture(request):
            if (
                trusted_audio(request.url)
                and urlsplit(request.url).hostname == "www.facebook.com"
            ):
                captured.append(request.url)

        page.context.on("request", capture)
        popup = None
        stage = "opening audio player"
        try:
            with page.expect_popup(timeout=5000) as opened:
                page.get_by_text("Hear this code", exact=True).click(timeout=3000)
            popup = opened.value
            popup.wait_for_url(
                re.compile(r"^https://www\.facebook\.com/captcha/tfbaudio/player/"),
                wait_until="commit",
                timeout=15000,
            )
            audio_deadline = min(deadline, time.monotonic() + 10)
            while not captured and time.monotonic() < audio_deadline:
                popup.wait_for_timeout(250)
            if not captured:
                log("Instagram native CAPTCHA audio unavailable")
                return False
            stage = "fetching audio"
            data = _audio(popup, captured[-1])
            remaining = deadline - time.monotonic()
            if remaining < 4:
                break
            stage = "transcribing audio"
            text = _transcribe(data, remaining - 3)
            answer = normalise_answer(text)
            if answer is None:
                log(
                    "Instagram native CAPTCHA transcription ambiguous; answer not submitted"
                )
                return False
            stage = "submitting answer"
            page.locator("textarea").fill(answer, timeout=3000)
            page.get_by_role("button", name="Next", exact=True).click(timeout=3000)
            page.wait_for_timeout(2000)
            if not present(page):
                log("Instagram native CAPTCHA form cleared")
                return True
            if attempt + 1 < MAX_ATTEMPTS and deadline - time.monotonic() > 5:
                page.get_by_text("Get a new code", exact=True).click(timeout=3000)
                page.wait_for_timeout(1500)
        except Exception as exc:  # noqa: BLE001 -- URLs/answers must not enter logs
            if stage == "submitting answer" and not present(page):
                log("Instagram native CAPTCHA form cleared after submission")
                return True
            log(
                f"Instagram native CAPTCHA failed during {stage} ({type(exc).__name__})"
            )
            return False
        finally:
            page.context.remove_listener("request", capture)
            if popup is not None:
                popup.close()
    log("Instagram native CAPTCHA not cleared within attempt budget")
    return False
