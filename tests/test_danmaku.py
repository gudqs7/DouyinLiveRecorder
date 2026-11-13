import struct
import time
import unittest
import zlib

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

    def test_packet_parser_handles_zlib_messages(self):
        message = (
            b'{"cmd":"DANMU_MSG","info":[[0,1,25,16777215],'
            b'"hello",[123,"user"]]}'
        )
        inner = _packet(version=0, operation=5, body=message)
        outer = _packet(version=2, operation=5, body=zlib.compress(inner))
        packets = danmaku._parse_bilibili_packets(outer)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0][0], 5)
        self.assertEqual(packets[0][1], message)

    def test_ass_layout_writes_rolling_dialogue(self):
        layout = danmaku.AssLayout(danmaku.AssStyle())
        line = layout.render_event(danmaku.DanmakuEvent(2.0, "hello", 0xFF0000))
        self.assertIn("Dialogue: 2,0:00:02.00,0:00:17.00,Float", line)
        self.assertIn("\\move(1920,0,", line)
        self.assertIn("\\c&H0000FF&", line)

    def test_ass_base_path_removes_segment_placeholder(self):
        path = r"D:\downloads\room_2026-01-01_00-00-00_%03d.ts"
        self.assertEqual(
            danmaku.ass_base_path(path),
            r"D:\downloads\room_2026-01-01_00-00-00",
        )


def _packet(version: int, operation: int, body: bytes) -> bytes:
    header = struct.pack(">IHHII", 16 + len(body), 16, version, operation, 1)
    return header + body


if __name__ == "__main__":
    unittest.main()
