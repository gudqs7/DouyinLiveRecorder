import time
import unittest

from src import danmaku


class DanmakuTests(unittest.TestCase):
    def test_format_ass_time(self):
        self.assertEqual(danmaku.format_ass_time(0), "0:00:00.00")
        self.assertEqual(danmaku.format_ass_time(3661.239), "1:01:01.24")
        self.assertEqual(danmaku.format_ass_time(59.999), "0:01:00.00")

    def test_media_clock_extrapolates_between_progress_updates(self):
        clock = danmaku.MediaClock(offset_seconds=0.5)
        self.assertIsNone(clock.now())
        clock.update(12.0)
        time.sleep(0.03)
        value = clock.now()
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 12.5)
        self.assertLess(value, 14.6)

    def test_ass_layout_writes_rolling_dialogue(self):
        layout = danmaku.AssLayout(danmaku.AssStyle())
        line = layout.render_event(danmaku.DanmakuEvent(2.0, "hello", 0xFF0000))
        self.assertIn("Dialogue: 2,0:00:02.00,0:00:17.00,Float", line)
        self.assertIn("\\move(1920,0,", line)
        self.assertIn("\\c&H0000FF&", line)

    def test_dense_danmaku_uses_overlap_instead_of_dropping(self):
        layout = danmaku.AssLayout(danmaku.AssStyle())
        events = [
            danmaku.DanmakuEvent(index / 100, f"dense-{index}")
            for index in range(200)
        ]
        lines = layout.render(events)
        self.assertEqual(len(lines), len(events))

    def test_ass_base_path_removes_segment_placeholder(self):
        path = r"D:\downloads\room_2026-01-01_00-00-00_%03d.ts"
        self.assertEqual(
            danmaku.ass_base_path(path),
            r"D:\downloads\room_2026-01-01_00-00-00",
        )

if __name__ == "__main__":
    unittest.main()
