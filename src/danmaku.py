# -*- coding: utf-8 -*-

"""Live danmaku capture and ASS subtitle generation.

Only Bilibili and Douyin are intentionally supported. Each recorder runs two
threads: one receives chat messages and one writes ASS dialogue lines.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import math
import os
import queue
import re
import struct
import subprocess
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests
import websocket

from . import douyin_danmaku

logger = logging.getLogger(__name__)

BILIBILI_ROOM_PATTERN = re.compile(r"live\.bilibili\.com/(\d+)")
DOUYIN_ROOM_PATTERN = re.compile(r"live\.douyin\.com/(\d+)")
PROXY_SCHEME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


def format_ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(seconds * 100)))
    hours, rem = divmod(centiseconds, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cents = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cents:02d}"


def ass_base_path(save_file_path: str) -> str:
    base_path = os.path.splitext(save_file_path)[0]
    return base_path.replace("_%03d", "")


def _normalise_proxy(proxy: str) -> Optional[str]:
    if not proxy or not proxy.strip():
        return None
    proxy = proxy.strip()
    if not PROXY_SCHEME_PATTERN.match(proxy):
        proxy = "http://" + proxy
    return proxy


def _websocket_proxy_kwargs(proxy: Optional[str]) -> dict:
    if not proxy:
        return {}
    parsed = urlparse(proxy)
    if not parsed.hostname or not parsed.port:
        return {}
    kwargs = {
        "http_proxy_host": parsed.hostname,
        "http_proxy_port": parsed.port,
    }
    if parsed.username is not None:
        kwargs["http_proxy_auth"] = (parsed.username, parsed.password or "")
    return kwargs


class MediaClock:
    """Map wall-clock time to FFmpeg's current output timestamp."""

    def __init__(self, offset_seconds: float = 0.0) -> None:
        self.offset_seconds = offset_seconds
        self._lock = threading.Lock()
        self._output_seconds = 0.0
        self._updated_at = time.monotonic()
        self._started = threading.Event()

    def update(self, output_seconds: float) -> None:
        if output_seconds < 0:
            return
        with self._lock:
            self._output_seconds = max(self._output_seconds, output_seconds)
            self._updated_at = time.monotonic()
        if output_seconds >= 0.05:
            self._started.set()

    def mark_started(self, output_seconds: float = 0.0) -> None:
        with self._lock:
            self._output_seconds = max(self._output_seconds, output_seconds)
            self._updated_at = time.monotonic()
        self._started.set()

    def wait_started(self, timeout: float) -> bool:
        return self._started.wait(timeout)

    def now(self) -> Optional[float]:
        if not self._started.is_set():
            return None
        with self._lock:
            output_seconds = self._output_seconds
            updated_at = self._updated_at
        # FFmpeg emits progress every 500 ms. Extrapolate between updates and
        # stop at two seconds to avoid drifting if the stream stalls.
        elapsed = min(max(0.0, time.monotonic() - updated_at), 2.0)
        return max(0.0, output_seconds + elapsed + self.offset_seconds)


@dataclass(frozen=True)
class AssStyle:
    width: int = 1920
    height: int = 1080
    font: str = "Microsoft YaHei"
    font_size: int = 42
    lane_size: int = 54
    duration: float = 15.0
    opacity: float = 0.7
    outline: float = 0.8
    width_ratio: float = 1.2
    horizontal_gap: float = 24.0
    float_percentage: float = 0.5
    time_offset: float = 0.0

    @classmethod
    def from_values(
        cls,
        font: str,
        font_size: int,
        duration: float,
        opacity: float,
        time_offset: float,
    ) -> "AssStyle":
        return cls(
            font=(font or "Microsoft YaHei").replace(",", " "),
            font_size=max(12, int(font_size)),
            duration=max(1.0, float(duration)),
            opacity=min(1.0, max(0.0, float(opacity))),
            time_offset=float(time_offset),
        )


@dataclass(frozen=True)
class DanmakuEvent:
    timestamp: float
    content: str
    color: int = 0xFFFFFF


@dataclass
class _Lane:
    last_shoot_time: float
    last_length: float


class AssLayout:
    """A compact Python port of danmu2ass's float-lane placement."""

    def __init__(self, style: AssStyle) -> None:
        self.style = style
        lane_count = max(
            1, int(style.float_percentage * style.height / style.lane_size)
        )
        self._lanes: list[Optional[_Lane]] = [None] * lane_count

    def header(self, title: str) -> str:
        alpha = int(round((1.0 - self.style.opacity) * 255))
        style = (
            f"Style: Float,{self.style.font},{self.style.font_size},"
            f"&H{alpha:02X}FFFFFF,&H00FFFFFF,&H{alpha:02X}000000,"
            f"&H00000000,0,0,0,0,100,100,0,0,1,"
            f"{self.style.outline:g},0,7,0,0,0,1"
        )
        safe_title = title.replace("\n", " ").replace("\r", " ")
        return (
            "[Script Info]\n"
            "; Script generated by DouyinLiveRecorder\n"
            f"Title: {safe_title}\n"
            "Script Updated By: DouyinLiveRecorder\n"
            "ScriptType: v4.00+\n"
            f"PlayResX: {self.style.width}\n"
            f"PlayResY: {self.style.height}\n"
            f"Aspect Ratio: {self.style.width}:{self.style.height}\n"
            "Collisions: Normal\n"
            "WrapStyle: 2\n"
            "ScaledBorderAndShadow: yes\n"
            "YCbCr Matrix: TV.601\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"{style}\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
        )

    def render_event(self, event: DanmakuEvent, local_start: float = 0.0) -> Optional[str]:
        timestamp = event.timestamp - local_start
        if timestamp < 0:
            return None
        content = _clean_ass_text(event.content)
        if not content:
            return None

        length = self._text_length(content)
        lane_index, timestamp = self._choose_lane(timestamp, length)
        if lane_index is None:
            return None

        y = lane_index * self.style.lane_size
        end = timestamp + self.style.duration
        r = (event.color >> 16) & 0xFF
        g = (event.color >> 8) & 0xFF
        b = event.color & 0xFF
        effect = (
            f"\\move({self.style.width},{y},{-(int(math.ceil(length)))},{y})"
        )
        return (
            f"Dialogue: 2,{format_ass_time(timestamp)},{format_ass_time(end)},"
            f"Float,,0,0,0,,{{{effect}\\c&H{b:02X}{g:02X}{r:02X}&}}{content}\n"
        )

    def render(
        self,
        events: Iterable[DanmakuEvent],
        local_start: float = 0.0,
        local_end: Optional[float] = None,
    ) -> list[str]:
        lines: list[str] = []
        for event in events:
            if event.timestamp < local_start:
                continue
            if local_end is not None and event.timestamp >= local_end:
                continue
            line = self.render_event(event, local_start)
            if line is not None:
                lines.append(line)
        return lines

    def _text_length(self, text: str) -> float:
        units = sum(2 if ord(char) < 128 else 3 for char in text)
        return self.style.font_size * units / 3 * self.style.width_ratio

    def _choose_lane(self, timestamp: float, length: float) -> tuple[Optional[int], float]:
        collisions: list[tuple[float, int]] = []
        for index, lane in enumerate(self._lanes):
            if lane is None:
                self._set_lane(index, timestamp, length)
                return index, timestamp

            collision, delay = self._collision(lane, timestamp, length)
            if collision in ("separate", "not_enough"):
                self._set_lane(index, timestamp, length)
                return index, timestamp
            collisions.append((delay, index))

        if collisions:
            delay, index = min(collisions)
            if delay < 1.0:
                timestamp += delay + 0.01
                self._set_lane(index, timestamp, length)
                return index, timestamp
        return None, timestamp

    def _set_lane(self, index: int, timestamp: float, length: float) -> None:
        self._lanes[index] = _Lane(timestamp, length)

    def _collision(
        self, lane: _Lane, timestamp: float, length: float
    ) -> tuple[str, float]:
        width = float(self.style.width)
        duration = self.style.duration
        gap = self.style.horizontal_gap
        delta_t = timestamp - lane.last_shoot_time
        speed_1 = (width + lane.last_length) / duration
        speed_2 = (width + length) / duration
        delta_x = speed_1 * delta_t - lane.last_length

        if delta_x < gap:
            if length <= lane.last_length:
                return "collide", (gap - delta_x) / speed_1
            delay = duration - (width - gap) / speed_2 - delta_t
            return "collide", delay

        if length <= lane.last_length:
            return "separate", 0.0
        position = speed_2 * (duration - delta_t)
        if position < width - gap:
            return "not_enough", 0.0
        return "collide", (position - (width - gap)) / speed_2


class DanmakuRecorder:
    """Receive live chat and write it to ASS using a shared video clock."""

    def __init__(
        self,
        platform: str,
        room_url: str,
        save_file_path: str,
        cookie: str,
        proxy: Optional[str],
        style: AssStyle,
    ) -> None:
        self.platform = platform
        self.room_url = room_url
        self.cookie = cookie or ""
        self.proxy = _normalise_proxy(proxy or "")
        self.style = style
        self.media_clock = MediaClock(style.time_offset)
        self.master_path = ass_base_path(save_file_path) + ".ass"
        self._queue: queue.Queue[Optional[DanmakuEvent]] = queue.Queue(maxsize=20000)
        self._events: list[DanmakuEvent] = []
        self._events_lock = threading.Lock()
        self._seen_ids: set[str] = set()
        self._seen_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None
        self._provider_thread: Optional[threading.Thread] = None
        self._finished = False

    @property
    def event_count(self) -> int:
        with self._events_lock:
            return len(self._events)

    def start(self) -> None:
        if self._writer_thread is not None:
            return
        self._writer_thread = threading.Thread(
            target=self._write_loop,
            name=f"danmaku-ass-{os.path.basename(self.master_path)}",
            daemon=True,
        )
        self._writer_thread.start()
        self._provider_thread = threading.Thread(
            target=self._provider_loop,
            name=f"danmaku-live-{self.platform}",
            daemon=True,
        )
        self._provider_thread.start()

    def add(self, content: str, color: int = 0xFFFFFF, message_id: str = "") -> None:
        if self._stop_event.is_set():
            return
        content = (content or "").strip()
        if not content:
            return
        if message_id:
            with self._seen_lock:
                if message_id in self._seen_ids:
                    return
                self._seen_ids.add(message_id)
                if len(self._seen_ids) > 100000:
                    self._seen_ids.clear()

        timestamp = self.media_clock.now()
        if timestamp is None:
            return
        event = DanmakuEvent(timestamp=timestamp, content=content, color=color)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("danmaku queue is full, dropping one message")

    def finish(
        self,
        segment_pattern: Optional[str] = None,
        fallback_segment_seconds: float = 1800.0,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._provider_thread is not None:
            self._provider_thread.join(timeout=5)
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=10)

        if self.event_count == 0:
            try:
                if os.path.exists(self.master_path):
                    os.remove(self.master_path)
            except OSError:
                pass
            return

        if segment_pattern and "%03d" in segment_pattern:
            self._write_segment_files(segment_pattern, fallback_segment_seconds)

    def _write_loop(self) -> None:
        layout = AssLayout(self.style)
        try:
            with open(self.master_path, "w", encoding="utf-8", newline="\n") as output:
                output.write(layout.header(os.path.basename(self.master_path)))
                output.flush()
                pending = 0
                while True:
                    try:
                        event = self._queue.get(timeout=1.0)
                    except queue.Empty:
                        output.flush()
                        continue
                    if event is None:
                        break
                    with self._events_lock:
                        self._events.append(event)
                    line = layout.render_event(event)
                    if line:
                        output.write(line)
                        pending += 1
                        if pending >= 20:
                            output.flush()
                            pending = 0
                output.flush()
        except Exception:
            logger.exception("write ASS file failed: %s", self.master_path)

    def _provider_loop(self) -> None:
        try:
            # Wait until FFmpeg has produced the first output timestamp. This
            # prevents pre-buffer chat from appearing before the recorded video.
            if not self.media_clock.wait_started(timeout=45):
                logger.warning("%s danmaku did not start: video clock unavailable", self.platform)
                return
            if self._stop_event.is_set():
                return
            if self.platform == "B站直播":
                provider = BilibiliDanmakuProvider(
                    self.room_url,
                    self.cookie,
                    self.proxy,
                    self.add,
                    self._stop_event,
                )
                provider.run()
            elif self.platform == "抖音直播":
                provider = DouyinDanmakuProvider(
                    self.room_url,
                    self.cookie,
                    self.proxy,
                    self.add,
                    self._stop_event,
                )
                provider.run()
        except Exception:
            logger.exception("%s danmaku capture stopped unexpectedly", self.platform)

    def _write_segment_files(
        self, segment_pattern: str, fallback_segment_seconds: float
    ) -> None:
        paths = sorted(glob.glob(segment_pattern.replace("%03d", "*")))
        with self._events_lock:
            events = list(self._events)
        if not paths or not events:
            return

        segment_start = 0.0
        for path in paths:
            duration = _probe_duration(path)
            if duration <= 0:
                duration = max(1.0, fallback_segment_seconds)
            segment_end = segment_start + duration
            layout = AssLayout(self.style)
            lines = layout.render(events, segment_start, segment_end)
            output_path = os.path.splitext(path)[0] + ".ass"
            try:
                with open(output_path, "w", encoding="utf-8", newline="\n") as output:
                    output.write(layout.header(os.path.basename(output_path)))
                    output.writelines(lines)
            except OSError:
                logger.exception("write segment ASS failed: %s", output_path)
            segment_start = segment_end


class BilibiliDanmakuProvider:
    def __init__(
        self,
        room_url: str,
        cookie: str,
        proxy: Optional[str],
        emit,
        stop_event: threading.Event,
    ) -> None:
        match = BILIBILI_ROOM_PATTERN.search(room_url)
        if match is None:
            raise ValueError(f"cannot parse Bilibili room id: {room_url}")
        self.room_id = match.group(1)
        self.cookie = cookie
        self.proxy = proxy
        self.emit = emit
        self.stop_event = stop_event

    def run(self) -> None:
        try:
            import aiohttp
            import blivedm
            import blivedm.models.web as web_models
        except ImportError:
            logger.warning("blivedm is unavailable, falling back to legacy Bilibili client")
            self._run_legacy()
            return

        owner = self

        class Handler(blivedm.BaseHandler):
            def _on_danmaku(handler_self, _client, message: web_models.DanmakuMessage):
                content = message.msg or ""
                message_id = f"{message.uid}:{message.timestamp}:{content}"
                owner.emit(
                    content,
                    message.color or 0xFFFFFF,
                    f"bili:{message_id}",
                )

            def _on_super_chat(handler_self, _client, message: web_models.SuperChatMessage):
                content = message.message or ""
                message_id = message.id or f"{message.uid}:{message.start_time}:{content}"
                owner.emit(
                    content,
                    0x2A9DF4,
                    f"bili-sc:{message_id}",
                )

        async def run_client():
            headers = {"Cookie": self.cookie} if self.cookie else None
            async with aiohttp.ClientSession(headers=headers) as session:
                client = blivedm.BLiveClient(
                    int(self.room_id),
                    session=session,
                    heartbeat_interval=10,
                )
                client.set_handler(Handler())
                client.start()
                try:
                    while not self.stop_event.is_set():
                        await asyncio.sleep(0.5)
                finally:
                    await client.stop_and_close()

        asyncio.run(run_client())

    def _run_legacy(self) -> None:
        retries = 0
        while not self.stop_event.is_set():
            try:
                self._connect_once()
                retries = 0
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                logger.warning("Bilibili danmaku reconnect after error: %s", exc)
            retries += 1
            self.stop_event.wait(min(1 + retries * 2, 20))

    def _connect_once(self) -> None:
        conf = self._get_conf()
        token = conf.get("token", "")
        hosts = conf.get("host_server_list") or []
        host = hosts[0]["host"] if hosts else conf.get("host", "broadcastlv.chat.bilibili.com")
        port = 443
        if hosts:
            host = hosts[0].get("host", host)
            port = int(hosts[0].get("wss_port", 443))
        url = f"wss://{host}:{port}/sub"
        headers = [
            f"User-Agent: {BILIBILI_USER_AGENT}",
            "Origin: https://live.bilibili.com",
            f"Referer: https://live.bilibili.com/{self.room_id}",
        ]
        if self.cookie:
            headers.append(f"Cookie: {self.cookie}")
        ws = websocket.create_connection(
            url,
            header=headers,
            timeout=10,
            **_websocket_proxy_kwargs(self.proxy),
        )
        try:
            ws.settimeout(1.0)
            auth = {
                "uid": 0,
                "roomid": int(self.room_id),
                "protover": 3,
                "platform": "web",
                "type": 2,
                "key": token,
            }
            body = json.dumps(auth, separators=(",", ":")).encode("utf-8")
            ws.send(_pack_bilibili_packet(7, body), opcode=websocket.ABNF.OPCODE_BINARY)
            last_heartbeat = 0.0
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now - last_heartbeat >= 30:
                    heartbeat = _pack_bilibili_packet(2, b"[object Object]")
                    ws.send(heartbeat, opcode=websocket.ABNF.OPCODE_BINARY)
                    last_heartbeat = now
                try:
                    payload = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    return
                if isinstance(payload, bytes):
                    self._handle_packet(payload)
        finally:
            ws.close()

    def _get_conf(self) -> dict:
        headers = {
            "User-Agent": BILIBILI_USER_AGENT,
            "Referer": f"https://live.bilibili.com/{self.room_id}",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        response = requests.get(
            "https://api.live.bilibili.com/room/v1/Danmu/getConf",
            params={"room_id": self.room_id, "platform": "pc", "player": "web"},
            headers=headers,
            proxies=proxies,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Bilibili danmu getConf failed: {data}")
        return data["data"]

    def _handle_packet(self, payload: bytes) -> None:
        for operation, body in _parse_bilibili_packets(payload):
            if operation in (8,):
                try:
                    reply = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if reply.get("code") not in (0, None):
                    raise RuntimeError(f"Bilibili danmaku auth failed: {reply}")
                continue
            if operation != 5:
                continue
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                continue
            command = data.get("cmd", "")
            if command.startswith("DANMU_MSG"):
                info = data.get("info") or []
                if len(info) < 2:
                    continue
                content = info[1]
                meta = info[0] if info[0] else []
                color = int(meta[3]) if len(meta) > 3 and str(meta[3]).isdigit() else 0xFFFFFF
                user = info[2] if len(info) > 2 and info[2] else []
                uid = str(user[0]) if user else "0"
                source_time = str(meta[4]) if len(meta) > 4 else str(time.time_ns())
                message_id = f"{uid}:{source_time}:{content}"
                self.emit(content, color, f"bili:{message_id}")
            elif command.startswith("SUPER_CHAT_MESSAGE"):
                message = data.get("data") or {}
                content = message.get("message", "")
                color = 0x2A9DF4
                message_id = str(message.get("id") or message.get("message_id") or content)
                self.emit(content, color, f"bili-sc:{message_id}")


class DouyinDanmakuProvider:
    def __init__(
        self,
        room_url: str,
        cookie: str,
        proxy: Optional[str],
        emit,
        stop_event: threading.Event,
    ) -> None:
        self.room_url = room_url
        self.cookie = cookie
        self.proxy = proxy
        self.emit = emit
        self.stop_event = stop_event

    def run(self) -> None:
        web_rid = self._extract_web_rid()
        if not web_rid:
            logger.warning("cannot resolve Douyin web room id: %s", self.room_url)
            return
        douyin_danmaku.set_douyin_cookie(self.cookie)
        asyncio.run(self._run_async(web_rid))

    def _extract_web_rid(self) -> str:
        match = DOUYIN_ROOM_PATTERN.search(self.room_url)
        if match:
            return match.group(1)
        headers = {"User-Agent": BILIBILI_USER_AGENT}
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            response = requests.get(
                self.room_url,
                headers=headers,
                proxies=proxies,
                timeout=15,
                allow_redirects=True,
            )
        except requests.RequestException:
            return ""
        match = DOUYIN_ROOM_PATTERN.search(response.url)
        return match.group(1) if match else ""

    async def _run_async(self, web_rid: str) -> None:
        handler = _DouyinDanmakuHandler(self.emit)
        client = douyin_danmaku.DouyinLiveClient(_DouyinRoomKey(web_rid))
        client.set_handler(handler)
        client.start()
        try:
            while not self.stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            await client.stop_and_close()


class _DouyinRoomKey:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class _DouyinDanmakuHandler:
    def __init__(self, emit) -> None:
        self.emit = emit

    def handle(self, _client, message: dict) -> None:
        if message.get("type") == "system":
            return
        method = message.get("method", "")
        if method not in ("WebcastChatMessage", "WebcastEmojiChatMessage"):
            return
        content = message.get("content") or message.get("defaultContent") or ""
        common = message.get("common") or {}
        message_id = str(common.get("msgId") or f"{content}:{time.time()}")
        self.emit(content, 0xFFFFFF, f"douyin:{message_id}")

    def on_client_stopped(self, _client, exception: Optional[Exception]) -> None:
        if exception is not None:
            logger.warning("Douyin danmaku client stopped: %s", exception)


def _pack_bilibili_packet(operation: int, body: bytes) -> bytes:
    header = struct.pack(">IHHII", 16 + len(body), 16, 1, operation, 1)
    return header + body


def _parse_bilibili_packets(payload: bytes) -> list[tuple[int, bytes]]:
    packets: list[tuple[int, bytes]] = []
    offset = 0
    while offset + 16 <= len(payload):
        packet_len, header_len, version, operation, _sequence = struct.unpack(
            ">IHHII", payload[offset:offset + 16]
        )
        if packet_len < header_len or offset + packet_len > len(payload):
            break
        body = payload[offset + header_len:offset + packet_len]
        offset += packet_len
        if version == 2:
            try:
                packets.extend(_parse_bilibili_packets(zlib.decompress(body)))
            except zlib.error:
                logger.warning("decompress Bilibili danmaku packet failed")
        elif version == 3:
            try:
                import brotli

                packets.extend(_parse_bilibili_packets(brotli.decompress(body)))
            except (ImportError, Exception):
                logger.warning("decompress brotli Bilibili danmaku packet failed")
        else:
            packets.append((operation, body))
    return packets


def _clean_ass_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\N")
    text = text.replace("{", "｛").replace("}", "｝")
    return text.strip()[:120]


def _probe_duration(path: str) -> float:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            check=False,
            creationflags=creation_flags,
            timeout=20,
        )
        return max(0.0, float(result.stdout.strip()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0
