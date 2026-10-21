"""Operator diagnostics must select only quality buttons actually offered."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from scripts.service_matrix_harness import _choose_offered_height, _Trace


class MatrixQualityTests(unittest.TestCase):
    def test_missing_requested_height_chooses_closest_lower_option(self):
        self.assertEqual(_choose_offered_height([1080, 720, 360], 480), 360)
        self.assertEqual(_choose_offered_height([1080, 480, 360], 480), 480)

    def test_source_above_target_chooses_smallest_actual_option(self):
        self.assertEqual(_choose_offered_height([1080, 720], 480), 720)

    def test_no_menu_yet_does_not_invent_selection(self):
        self.assertIsNone(_choose_offered_height([], 480))


class MatrixQualityMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_real_callbacks_per_token_including_locked_options(self):
        trace = _Trace(time.monotonic(), "youtube")
        method = SimpleNamespace(
            __api_method__="editMessageText", text="quality menu",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="720p 🔒", callback_data="qh:one:720")],
                [InlineKeyboardButton(text="1080p", callback_data="qh:one:1080")],
                [InlineKeyboardButton(text="360p", callback_data="qh:two:360")],
                [InlineKeyboardButton(text="480p", callback_data="cancel:one")],
            ]),
        )
        await trace(AsyncMock(return_value=SimpleNamespace(message_id=1)), None, method)
        self.assertEqual(trace.quality_offers, {"one": [720, 1080], "two": [360]})
        self.assertEqual(trace.quality_locked, {"one": {720}})
        self.assertEqual(_choose_offered_height(trace.quality_candidates("one"), 480), 1080)

    async def test_locked_requested_quality_prefers_lower_unlocked_option(self):
        trace = _Trace(time.monotonic(), "pornhub")
        method = SimpleNamespace(
            __api_method__="editMessageText", text="quality menu",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="480p 🔒", callback_data="qh:one:480")],
                [InlineKeyboardButton(text="240p", callback_data="qh:one:240")],
            ]),
        )
        await trace(AsyncMock(return_value=SimpleNamespace(message_id=1)), None, method)
        self.assertEqual(_choose_offered_height(trace.quality_candidates("one"), 480), 240)

    def test_all_locked_keeps_actual_options_for_real_handler_rejection(self):
        trace = _Trace(time.monotonic(), "pornhub")
        trace.quality_offers["one"] = [720, 480]
        trace.quality_locked["one"] = {720, 480}
        self.assertEqual(_choose_offered_height(trace.quality_candidates("one"), 480), 480)
