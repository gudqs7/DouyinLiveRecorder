# -*- coding: utf-8 -*-
"""抖音弹幕客户端

从 douyinLive 的 Go 实现移植而来：https://github.com/jwwsjlm/douyinLive
流程：请求直播页拿房间信息 -> 握手签名 -> 连抖音 WebSocket -> 解 protobuf 弹幕。
输出给消息处理器的字典结构和 douyinLive 服务下发的 JSON 保持一致。
"""
import asyncio
import base64
import gzip
import hashlib
import json
import logging
import os
import random
import re
import time
from typing import *
from urllib.parse import unquote

import aiohttp

logger = logging.getLogger(__name__)

_configured_cookie = ''

LIVE_PAGE_URL = 'https://live.douyin.com/'
# 弹幕里的小表情列表接口，见抖音 web 端的 /aweme/v1/web/emoji/list
EMOJI_LIST_URL = 'https://live.douyin.com/aweme/v1/web/emoji/list/'
EMOJI_LIST_CACHE_SECONDS = 24 * 3600
# 参考 constants.go，im/fetch 不可用时用的兜底推送地址
WEBCAST_PUSH_URL = 'wss://webcast100-ws-web-lf.douyin.com/webcast/im/push/v2/'
WEBCAST_SDK_VERSION = '1.0.15'
WEBCAST_APP_NAME = 'douyin_web'
WEBCAST_VERSION_CODE = '180800'
WEBCAST_AID = '6383'
WEBCAST_LIVE_ID = '1'
WEBCAST_DID_RULE = '3'
WEBCAST_HOST = 'https://live.douyin.com'
# 参考 protocol_profile.go，Web 画像的 UA 主版本
CHROME_MAJOR_VERSION = '133'
USER_AGENT = (
    f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
    f'Chrome/{CHROME_MAJOR_VERSION}.0.0.0 Safari/537.36'
)
REFERER = 'https://live.douyin.com/'

# 未开播/连接断开后的重试间隔
RECONNECT_MAX_INTERVAL = 20
OFFLINE_POLL_INTERVAL = 30
# 上游超时没消息就重连，参考 wsReadTimeout
READ_TIMEOUT = 70
# 上游偶尔只继续发心跳、业务推送实际已失效，长时间没有 msg/gz 就主动重连
BUSINESS_TIMEOUT = 180
WRITE_TIMEOUT = 10
MAX_MESSAGE_SIZE = 16 << 20

# 直播结束控制消息的 action，参考 controlActionLiveEnd
CONTROL_ACTION_LIVE_END = 3

# 直播间状态码
STATUS_OFFLINE = 'ROOM_OFFLINE'
STATUS_ONLINE = 'ROOM_ONLINE'
STATUS_ENDED = 'ROOM_ENDED'
STATUS_NOT_FOUND = 'ROOM_NOT_FOUND'
STATUS_UNKNOWN = 'ROOM_STATUS_UNKNOWN'


class _LiveEnded(Exception):
    """收到直播结束控制消息"""


# 抖音小表情列表的缓存
_emoji_list_cache: Optional[List[dict]] = None
_emoji_list_cache_time = 0.0

_EMOJI_LIST_PARAMS = {
    'device_platform': 'webapp', 'aid': WEBCAST_AID, 'channel': 'channel_pc_web',
    'pc_client_type': '1', 'version_code': '170400', 'version_name': '17.4.0',
    'cookie_enabled': 'true', 'screen_width': '1920', 'screen_height': '1080',
    'browser_language': 'zh-CN', 'browser_platform': 'Win32', 'browser_name': 'Chrome',
    'browser_version': f'{CHROME_MAJOR_VERSION}.0.0.0', 'browser_online': 'true',
    'engine_name': 'Blink', 'engine_version': f'{CHROME_MAJOR_VERSION}.0.0.0',
    'os_name': 'Windows', 'os_version': '10', 'cpu_core_num': '8', 'device_memory': '8',
    'platform': 'PC', 'downlink': '10', 'effective_type': '4g', 'round_trip_time': '50',
    # need_all=true 才会返回全部表情（含粉丝团/活动表情），网页端也是这么传的
    'need_all': 'true',
}


def _parse_cookie_header(cookie: str) -> Dict[str, str]:
    """把浏览器复制出来的Cookie解析成字段字典，保留原值不做URL编码转换"""
    result: Dict[str, str] = {}
    # 浏览器复制出来的Cookie可能带换行、末尾分号、重复字段；统一按;或换行切分
    for item in re.split(r'[;\r\n]+', cookie):
        item = item.strip()
        if item == '' or '=' not in item:
            continue
        name, value = item.split('=', 1)
        name = name.strip()
        if name == '':
            continue
        # 后出现的同名字段覆盖先出现的，浏览器复制出重复字段时以最后一个为准
        result[name] = value.strip()
    return result


def normalize_cookie(cookie: str) -> str:
    """规范化Cookie头，便于写进请求头"""
    if not cookie:
        return ''
    cookie = cookie.strip()
    if cookie.lower().startswith('cookie:'):
        cookie = cookie[len('cookie:'):].strip()
    return '; '.join(f'{name}={value}' for name, value in _parse_cookie_header(cookie).items())


def merge_cookies(*cookies: str) -> str:
    """合并多个Cookie头，后面的字段覆盖前面的同名字段"""
    merged: Dict[str, str] = {}
    for cookie in cookies:
        if cookie:
            merged.update(_parse_cookie_header(cookie))
    return '; '.join(f'{name}={value}' for name, value in merged.items())


def set_douyin_cookie(cookie: str) -> None:
    """Set the cookie supplied by DouyinLiveRecorder's config.ini."""
    global _configured_cookie
    _configured_cookie = normalize_cookie(cookie)


def get_douyin_cookie() -> str:
    """按优先级读取抖音Cookie：环境变量 > 程序配置"""
    env_cookie = os.environ.get('DOUYIN_COOKIE', '')
    if env_cookie.strip() != '':
        return normalize_cookie(env_cookie)

    return _configured_cookie


def get_douyin_cookie_expire_time(cookie: str = '') -> Optional[int]:
    """从Cookie里的sid_guard解析过期时间，返回Unix秒；解析不出返回None

    sid_guard示例（URL编码前）：
    0bbf...|1790470578|5184000|Thu, 26 Nov 2026 00:56:18 GMT
    其中 1790470578 是签发时间，5184000 是有效秒数，两者相加就是过期时间。
    """
    if cookie == '':
        cookie = get_douyin_cookie()
    fields = _parse_cookie_header(cookie)
    sid_guard = fields.get('sid_guard', '')
    if sid_guard == '':
        return None

    value = unquote(sid_guard)
    parts = value.split('|')
    if len(parts) < 3:
        return None
    try:
        issued_at = int(parts[1])
        max_age = int(parts[2])
    except (TypeError, ValueError):
        return None

    if issued_at <= 0 or max_age <= 0:
        return None
    # 兼容毫秒时间戳
    if issued_at > 10 ** 12:
        issued_at //= 1000
    return issued_at + max_age


def parse_emoji_list(data: dict) -> List[dict]:
    """把抖音表情接口的响应转成blivechat的文本表情格式"""
    emoticons = []
    for emoji in data.get('emoji_list') or []:
        keyword = emoji.get('display_name') or ''
        url_list = (emoji.get('emoji_url') or {}).get('url_list') or []
        if keyword != '' and url_list:
            emoticons.append({'keyword': keyword, 'url': url_list[0]})
    return emoticons


async def get_text_emoticons() -> List[dict]:
    """收集抖音弹幕里的小表情，返回blivechat的文本表情格式

    弹幕里的表情就是[名字]这样的纯文本，配好名字到图片的映射前端才能渲染成图片。
    """
    global _emoji_list_cache, _emoji_list_cache_time
    if _emoji_list_cache is not None and time.time() - _emoji_list_cache_time < EMOJI_LIST_CACHE_SECONDS:
        return _emoji_list_cache

    try:
        cookie = get_douyin_cookie()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            headers = {
                'User-Agent': USER_AGENT,
                'Referer': REFERER,
            }
            if cookie != '':
                headers['Cookie'] = cookie
            async with session.get(EMOJI_LIST_URL, params=_EMOJI_LIST_PARAMS, headers=headers) as res:
                data = await res.json(content_type=None)
    except Exception:
        logger.exception('fetch douyin emoji list failed')
        return _emoji_list_cache or []

    emoticons = parse_emoji_list(data)
    if emoticons:
        _emoji_list_cache = emoticons
        _emoji_list_cache_time = time.time()
        logger.info('fetched %d douyin text emoticons', len(emoticons))
    return _emoji_list_cache or []


# ------------------------------------------------------------------------------
# WebSocket 握手签名，移植自 internal/webcastsign/native.go
# ------------------------------------------------------------------------------

# 抖音把标准base64换成了自定义字母表
_SIGN_ALPHABET = 'Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe'
_SIGN_TABLE = str.maketrans(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/', _SIGN_ALPHABET
)
_EMPTY_BODY_DIGEST = hashlib.md5(hashlib.md5(b'').digest()).digest()
_WEBSOCKET_MODE_FLAG = 1 << 6


def _rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]

    result = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        result.append(byte ^ state[(state[i] + state[j]) & 0xFF])
    return bytes(result)


def _sign_base64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii').rstrip('=').translate(_SIGN_TABLE)


def make_websocket_signature(x_ms_stub: str, counter: int, random_flag: bool,
                             payload_random: int, key_random: int) -> str:
    """用显式状态值生成签名，方便和 Go 的测试向量对齐"""
    stub_digest = hashlib.md5(bytes.fromhex(x_ms_stub)).digest()
    payload = bytearray([
        counter & 0x3F,
        0,
        1,  # environmentCode
        14,  # userBehaviorCode
        _EMPTY_BODY_DIGEST[14],
        _EMPTY_BODY_DIGEST[15],
        stub_digest[14],
        stub_digest[15],
        payload_random,
    ])
    checksum = 0
    for byte in payload:
        checksum ^= byte
    payload.append(checksum)

    flags = _WEBSOCKET_MODE_FLAG
    if random_flag:
        flags |= 1 << 4
    encrypted = _rc4(bytes([key_random]), bytes(payload))
    return _sign_base64(bytes([flags, key_random]) + encrypted)


def make_signature_params(room_id: str, user_unique_id: str) -> str:
    """X-MS-STUB 的签名输入，顺序不能改"""
    return ','.join(f'{key}={value}' for key, value in [
        ('live_id', WEBCAST_LIVE_ID),
        ('aid', WEBCAST_AID),
        ('version_code', WEBCAST_VERSION_CODE),
        ('webcast_sdk_version', WEBCAST_SDK_VERSION),
        ('room_id', room_id),
        ('sub_room_id', ''),
        ('sub_channel_id', ''),
        ('did_rule', WEBCAST_DID_RULE),
        ('user_unique_id', user_unique_id),
        ('device_platform', 'web'),
        ('device_type', ''),
        ('ac', ''),
        ('identity', 'audience'),
    ])


class DouyinSigner:
    """一次会话内的签名序号必须递增，和 Go 的 Generator 一致"""
    def __init__(self):
        self._counter = 0

    def sign(self, room_id: str, user_unique_id: str) -> str:
        x_ms_stub = hashlib.md5(make_signature_params(room_id, user_unique_id).encode()).hexdigest()
        self._counter = (self._counter + 1) & 0x3F
        return make_websocket_signature(
            x_ms_stub, self._counter, random.getrandbits(1) == 1, random.randrange(255), random.randrange(255)
        )


# ------------------------------------------------------------------------------
# 极简protobuf读取，抖音IM用到的字段只有varint和length-delimited
# ------------------------------------------------------------------------------

class _ProtoBuffer:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.fields: Dict[int, list] = {}

    def parse(self) -> Dict[int, list]:
        while self._pos < len(self._data):
            key = self._read_varint()
            field_number = key >> 3
            wire_type = key & 0x7
            if wire_type == 0:
                value = self._read_varint()
            elif wire_type == 2:
                length = self._read_varint()
                value = self._data[self._pos:self._pos + length]
                self._pos += length
            elif wire_type == 5:
                value = self._data[self._pos:self._pos + 4]
                self._pos += 4
            elif wire_type == 1:
                value = self._data[self._pos:self._pos + 8]
                self._pos += 8
            else:
                raise ValueError(f'Unsupported protobuf wire type {wire_type}')
            self.fields.setdefault(field_number, []).append(value)
        return self.fields

    def _read_varint(self) -> int:
        result = 0
        shift = 0
        while True:
            if self._pos >= len(self._data):
                raise ValueError('Truncated protobuf varint')
            byte = self._data[self._pos]
            self._pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7


def parse_protobuf(data: bytes) -> Dict[int, list]:
    return _ProtoBuffer(data).parse()


def pb_int(fields: dict, number: int, default: int = 0) -> int:
    values = fields.get(number)
    if not values:
        return default
    value = values[0]
    return value if isinstance(value, int) else default


def pb_str(fields: dict, number: int, default: str = '') -> str:
    values = fields.get(number)
    if not values:
        return default
    value = values[0]
    if not isinstance(value, bytes):
        return default
    return value.decode('utf-8', 'replace')


def pb_bytes(fields: dict, number: int, default: bytes = b'') -> bytes:
    values = fields.get(number)
    return values[0] if values and isinstance(values[0], bytes) else default


def pb_message(fields: dict, number: int) -> dict:
    data = pb_bytes(fields, number)
    return parse_protobuf(data) if data else {}


def pb_messages(fields: dict, number: int) -> List[dict]:
    return [parse_protobuf(value) for value in fields.get(number, []) if isinstance(value, bytes)]


def _pb_varint(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result.append(byte | 0x80)
        else:
            result.append(byte)
            return bytes(result)


def _pb_varint_field(number: int, value: int) -> bytes:
    return _pb_varint(number << 3) + _pb_varint(value)


def _pb_bytes_field(number: int, value: bytes) -> bytes:
    return _pb_varint((number << 3) | 2) + _pb_varint(len(value)) + value


def make_heartbeat_frame() -> bytes:
    """PushFrame{payload_type: "hb"}"""
    return _pb_bytes_field(7, b'hb')


def make_ack_frame(log_id: int, internal_ext: str) -> bytes:
    """PushFrame{log_id, payload_type: "ack", payload: internal_ext}"""
    return (
        _pb_varint_field(2, log_id)
        + _pb_bytes_field(7, b'ack')
        + _pb_bytes_field(8, internal_ext.encode('utf-8'))
    )


# ------------------------------------------------------------------------------
# protobuf消息 -> 和douyinLive服务一致的字典结构
# ------------------------------------------------------------------------------

def _decode_image(fields: dict) -> dict:
    return {'urlList': [url.decode('utf-8', 'replace') for url in fields.get(1, []) if isinstance(url, bytes)]}


def _decode_user(fields: dict) -> dict:
    if not fields:
        return {}
    user_id = pb_int(fields, 1)
    if user_id == 0:
        user_id = pb_int(fields, 2)
    fans_club_data = pb_message(pb_message(fields, 24), 1)
    return {
        'id': str(user_id),
        'shortId': str(pb_int(fields, 2)),
        'nickname': pb_str(fields, 3),
        'avatarThumb': _decode_image(pb_message(fields, 9)),
        'payGrade': {'level': pb_int(pb_message(fields, 23), 6)},
        'fansClub': {'data': {
            'clubName': pb_str(fans_club_data, 1),
            'level': pb_int(fans_club_data, 2),
        }},
    }


def _decode_common(fields: dict) -> dict:
    return {
        'method': pb_str(fields, 1),
        'msgId': str(pb_int(fields, 2)),
        'roomId': str(pb_int(fields, 3)),
        'createTime': str(pb_int(fields, 4)),
    }


def _decode_piece(fields: dict) -> dict:
    piece = {'type': pb_int(fields, 1)}
    string_value = pb_str(fields, 11)
    if string_value != '':
        piece['stringValue'] = string_value
    schema_key = pb_str(fields, 100)
    if schema_key != '':
        piece['schemaKey'] = schema_key
    image = pb_message(pb_message(fields, 25), 1)
    if image:
        piece['imageValue'] = {'image': _decode_image(image)}
    return piece


def decode_im_message(method: str, payload: bytes) -> Optional[dict]:
    """把单条IM消息的protobuf载荷转成字典，不关心的消息返回None"""
    if method not in _MESSAGE_DECODERS:
        return None
    fields = parse_protobuf(payload)
    message = _MESSAGE_DECODERS[method](fields)
    message['method'] = method
    return message


def _decode_chat_message(fields: dict) -> dict:
    message = {
        'common': _decode_common(pb_message(fields, 1)),
        'user': _decode_user(pb_message(fields, 2)),
        'content': pb_str(fields, 3),
        'eventTime': str(pb_int(fields, 15)),
    }
    # 合成(融合)表情、富文本内容放在rtf_content里
    rtf_content = pb_message(fields, 41) or pb_message(fields, 40)
    if rtf_content:
        message['rtfContent'] = {'pieces': [_decode_piece(piece) for piece in pb_messages(rtf_content, 4)]}
    return message


def _decode_emoji_chat_message(fields: dict) -> dict:
    return {
        'common': _decode_common(pb_message(fields, 1)),
        'user': _decode_user(pb_message(fields, 2)),
        'defaultContent': pb_str(fields, 5),
        'emojiContent': {'pieces': [_decode_piece(piece) for piece in pb_messages(pb_message(fields, 4), 4)]},
    }


def _decode_gift_message(fields: dict) -> dict:
    gift = pb_message(fields, 15)
    gift_id = pb_int(fields, 2) or pb_int(gift, 5)
    return {
        'common': _decode_common(pb_message(fields, 1)),
        'user': _decode_user(pb_message(fields, 7)),
        'giftId': str(gift_id),
        'repeatCount': str(pb_int(fields, 5)),
        'comboCount': str(pb_int(fields, 6)),
        # 抖音礼物proto里的连击结束、组合id、总数量；部分端侧消息只有其中的一部分
        'repeatEnd': str(pb_int(fields, 9)),
        'groupId': str(pb_int(fields, 11)),
        'totalCount': str(pb_int(fields, 14)),
        'count': str(pb_int(fields, 14) or pb_int(fields, 44)),
        'gift': {
            'id': str(pb_int(gift, 5) or gift_id),
            'name': pb_str(gift, 16) or pb_str(gift, 2),
            'diamondCount': pb_int(gift, 12),
            'image': _decode_image(pb_message(gift, 1)),
            'icon': _decode_image(pb_message(gift, 21)),
        },
    }


def _first_nonempty_image(*images: dict) -> dict:
    for image in images:
        if image.get('urlList'):
            return image
    return {'urlList': []}


def _decode_light_gift_message(fields: dict) -> dict:
    """新版直播间的轻礼物消息，用户信息在 common.user 里。"""
    common = pb_message(fields, 1)
    gift_info = pb_message(fields, 7)
    gift_struct = pb_message(fields, 13)
    return {
        'common': _decode_common(common),
        'user': _decode_user(pb_message(common, 15)),
        'senderId': str(pb_int(fields, 5)),
        'giftId': str(pb_int(gift_info, 1) or pb_int(gift_struct, 5)),
        'repeatCount': str(pb_int(fields, 3)),
        'comboCount': str(pb_int(fields, 4)),
        'count': str(pb_int(fields, 10)),
        'gift': {
            'id': str(pb_int(gift_struct, 5) or pb_int(gift_info, 1)),
            'name': pb_str(gift_struct, 16) or pb_str(gift_struct, 2),
            'diamondCount': pb_int(gift_info, 3) or pb_int(gift_struct, 12),
            'image': _first_nonempty_image(
                _decode_image(pb_message(gift_struct, 1)),
                _decode_image(pb_message(gift_info, 2)),
            ),
            'icon': _first_nonempty_image(
                _decode_image(pb_message(gift_struct, 21)),
                _decode_image(pb_message(gift_info, 2)),
            ),
        },
    }


def _decode_control_message(fields: dict) -> dict:
    return {
        'common': _decode_common(pb_message(fields, 1)),
        'action': pb_int(fields, 2),
    }


def _decode_member_message(fields: dict) -> dict:
    """只取用户资料，用于给不带昵称的轻礼物补齐昵称/头像"""
    return {
        'common': _decode_common(pb_message(fields, 1)),
        'user': _decode_user(pb_message(fields, 2)),
    }


# def _decode_fansclub_message(fields: dict) -> dict:
#     """粉丝团消息按“点亮粉丝团”礼物展示"""
#     return {
#         'common': _decode_common(pb_message(fields, 1)),
#         'user': _decode_user(pb_message(fields, 4)),
#         'gift': {
#             'id': 0,
#             'name': '点亮粉丝团',
#             'diamondCount': 0,
#             'image': {'urlList': [
#                 'https://p11-webcast.douyinpic.com/img/webcast/5ddfcd51beaa7cad1294a4e517bc80fb.png~tplv-obj.png'
#             ]},
#             'icon': {},
#         },
#         'repeatCount': '1',
#         'count': '1',
#     }


_MESSAGE_DECODERS: Dict[str, Callable[[dict], dict]] = {
    'WebcastChatMessage': _decode_chat_message,
    'WebcastEmojiChatMessage': _decode_emoji_chat_message,
    'WebcastGiftMessage': _decode_gift_message,
    'WebcastLightGiftMessage': _decode_light_gift_message,
#     'WebcastFansclubMessage': _decode_fansclub_message,
    'WebcastMemberMessage': _decode_member_message,
    'WebcastControlMessage': _decode_control_message,
}


# ------------------------------------------------------------------------------
# 直播页解析
# ------------------------------------------------------------------------------

def _match_braces(text: str, start: int) -> str:
    """从start位置的'{'开始找配对的'}', 跳过字符串里的花括号"""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ''


def _object_of(text: str, key: str) -> str:
    index = text.find(f'"{key}":')
    if index < 0:
        return ''
    brace = text.find('{', index + len(key) + 3)
    if brace < 0:
        return ''
    return _match_braces(text, brace)


def _text_field(text: str, key: str) -> str:
    match = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % re.escape(key), text)
    if match is None:
        return ''
    try:
        return json.loads('"' + match.group(1) + '"')
    except ValueError:
        return ''


def _int_field(text: str, key: str, default: int = 0) -> int:
    match = re.search(r'"%s"\s*:\s*(-?\d+)' % re.escape(key), text)
    return int(match.group(1)) if match is not None else default


def _first_image_url(image_text: str) -> str:
    urls = re.findall(r'"(https?://[^"\\]+)"', image_text)
    return urls[-1] if urls else ''


def parse_live_page(html: str) -> Optional[dict]:
    """从直播页SSR里提取房间信息，参考 room_info.go 的 parseRoomInfoFromLivePage"""
    plain = html.replace('\\"', '"').replace('\\u0026', '&')
    marker = '"roomStore":{"roomInfo":'
    search_from = 0
    while True:
        index = plain.find(marker, search_from)
        if index < 0:
            return None
        search_from = index + 1

        brace = plain.find('{', index + len('"roomStore":'))
        room_store = _match_braces(plain, brace) if brace >= 0 else ''
        if room_store == '':
            continue

        room_text = _object_of(room_store, 'room')
        anchor_text = _object_of(room_store, 'anchor')
        room_id = _text_field(room_text, 'id_str') or str(_int_field(room_text, 'id') or '')
        if room_id == '':
            room_id = str(_int_field(room_text, 'id') or '')
        nickname = _text_field(anchor_text, 'nickname')
        if room_id == '' and nickname == '':
            continue

        user_unique_id = ''
        match = re.search(r'user_unique_id[^0-9]{0,12}(\d{10,25})', html)
        if match is not None:
            user_unique_id = match.group(1)

        return {
            'room_id': room_id,
            'user_unique_id': user_unique_id,
            'live': _int_field(room_text, 'status', -1) == 2,
            'title': _text_field(room_text, 'title'),
            'live_name': nickname,
            'avatar_thumb': _first_image_url(_object_of(anchor_text, 'avatar_thumb')),
        }
    # 到不了这里


# ------------------------------------------------------------------------------
# 客户端
# ------------------------------------------------------------------------------

class DouyinLiveClient:
    """到抖音的弹幕连接，接口和blivedm的客户端保持一致"""
    def __init__(self, room_key):
        self._room_key = room_key
        self._handler: Optional[Any] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._network_future: Optional[asyncio.Future] = None
        self._signer = DouyinSigner()

        # 直播页拿到的信息
        self._room_id = ''
        self._user_unique_id = ''
        self._live_name = ''
        self._title = ''
        self._avatar_thumb = ''
        self._cookie_header = ''
        # 建连后下游返回的游标
        self._cursor = ''
        self._internal_ext = ''
        self._push_url = ''
        # 见过的用户资料，轻礼物只带发送者 id 时靠它补昵称/头像
        self._user_cache: Dict[str, dict] = {}

        # 上一次上报的直播状态，避免重复提示
        self.last_status_code: Optional[str] = None

    @property
    def room_key(self):
        return self._room_key

    @property
    def room_id(self):
        return self._room_key.value

    @property
    def is_running(self) -> bool:
        return self._network_future is not None

    def set_handler(self, handler):
        self._handler = handler

    def start(self):
        if self.is_running:
            logger.warning('room=%s client is running, cannot start() again', self.room_key)
            return

        self._network_future = asyncio.create_task(self._network_coroutine_wrapper())

    async def stop_and_close(self):
        if self.is_running:
            self._network_future.cancel()
            try:
                await self._network_future
            except asyncio.CancelledError:
                pass

        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _network_coroutine_wrapper(self):
        exception = None
        try:
            await self._network_coroutine()
        except asyncio.CancelledError:
            # 正常停止
            pass
        except Exception as e:
            logger.exception('room=%s douyin network coroutine finished with exception:', self.room_key)
            exception = e
        finally:
            logger.debug('room=%s douyin network coroutine finished', self.room_key)
            self._network_future = None

        if self._handler is not None:
            self._handler.on_client_stopped(self, exception)

    async def _network_coroutine(self):
        total_retry_count = 0
        while True:
            try:
                await self._connect_once()
                # 连接成功过就重新计数
                total_retry_count = 0
            except asyncio.CancelledError:
                raise
            except _LiveEnded:
                total_retry_count = 0
            except Exception:
                logger.exception('room=%s connect failed:', self.room_key)

            total_retry_count += 1
            # 加随机延迟，防止同时请求导致雪崩
            await asyncio.sleep(min(1 + total_retry_count * 2, RECONNECT_MAX_INTERVAL) + random.uniform(0, 3))

    async def _connect_once(self):
        if self._session is None:
            # 不要用CookieJar自动管理：否则同名字段可能覆盖我们手动拼接的配置Cookie
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=15),
            )

        room = await self._fetch_room_info()
        if room is None:
            # 可能是验证码、页面结构变化或房间号无效，重试而不是直接报错
            await asyncio.sleep(OFFLINE_POLL_INTERVAL)
            return
        if not room['live']:
            text = '抖音直播间未开播，等待开播'
            self.send_live_status(STATUS_OFFLINE, text, False)
            await asyncio.sleep(OFFLINE_POLL_INTERVAL)
            return

        self.send_live_status(STATUS_ONLINE, '抖音直播间已开播', True)
        await self._fetch_initial_im_state()
        await self._run_websocket()

    async def _fetch_room_info(self):
        """请求抖音首页拿Cookie，再请求直播页拿房间信息"""
        headers = {
            'User-Agent': USER_AGENT,
            'Referer': REFERER,
            'Accept': 'text/html,application/xhtml+xml',
        }
        auto_cookie = ''
        async with self._session.get(LIVE_PAGE_URL, headers=headers) as res:
            cookies = res.cookies
            auto_cookie = '; '.join(f'{name}={cookie.value}' for name, cookie in cookies.items())

        configured_cookie = get_douyin_cookie()
        # 以配置Cookie为主，同时补齐首页自动拿到的ttwid/msToken等匿名Cookie
        self._cookie_header = merge_cookies(auto_cookie, configured_cookie)
        if configured_cookie != '':
            logger.info(
                'room=%s using configured douyin cookie, fields=%d',
                self.room_key, len(_parse_cookie_header(configured_cookie)),
            )

        page_headers = {
            'User-Agent': USER_AGENT,
            'Referer': REFERER,
            'Cookie': self._cookie_header,
            'Accept': 'text/html,application/xhtml+xml',
        }
        url = LIVE_PAGE_URL + self.room_id
        async with self._session.get(url, headers=page_headers) as res:
            html = (await res.read()).decode('utf-8', 'replace')

        room = parse_live_page(html)
        if room is None or (room['room_id'] == '' and room['live_name'] == ''):
            self.send_live_status(STATUS_UNKNOWN, '暂时无法获取抖音直播间信息，请检查房间号，重试中', False)
            return None

        if room['user_unique_id'] == '':
            # 抖音新版直播页不再总是下发 user_unique_id。匿名 Web 客户端
            # 可以自行生成一个稳定的 19 位用户标识参与签名。
            room['user_unique_id'] = str(random.randrange(10 ** 18, 10 ** 19))

        self._room_id = room['room_id']
        self._user_unique_id = room['user_unique_id']
        self._live_name = room['live_name']
        self._title = room['title']
        self._avatar_thumb = room['avatar_thumb']
        return room

    async def _fetch_initial_im_state(self):
        """按网页版流程先请求 im/fetch，获取真实 cursor、internal_ext 和推送地址"""
        ms_token = _parse_cookie_header(self._cookie_header).get('msToken', '')
        params = [
            ('resp_content_type', 'protobuf'),
            ('did_rule', WEBCAST_DID_RULE),
            ('device_id', ''),
            ('app_name', WEBCAST_APP_NAME),
            ('endpoint', 'live_pc'),
            ('support_wrds', '1'),
            ('user_unique_id', self._user_unique_id),
            ('identity', 'audience'),
            ('need_persist_msg_count', '15'),
            ('insert_task_id', ''),
            ('live_reason', ''),
            ('room_id', self._room_id),
            ('version_code', WEBCAST_VERSION_CODE),
            ('last_rtt', '0'),
            ('live_id', WEBCAST_LIVE_ID),
            ('aid', WEBCAST_AID),
            ('fetch_rule', '1'),
            ('cursor', ''),
            ('internal_ext', ''),
            ('device_platform', 'web'),
            ('cookie_enabled', 'true'),
            ('screen_width', '1920'),
            ('screen_height', '1080'),
            ('browser_language', 'zh-CN'),
            ('browser_platform', 'Win32'),
            ('browser_name', 'Mozilla'),
            ('browser_version', USER_AGENT.split('Mozilla/', 1)[-1]),
            ('browser_online', 'true'),
            ('tz_name', 'Asia/Shanghai'),
            ('msToken', ms_token),
        ]
        headers = {
            'User-Agent': USER_AGENT,
            'Referer': REFERER,
            'Cookie': self._cookie_header,
            'Accept': '*/*',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }

        try:
            async with self._session.get(
                    LIVE_PAGE_URL + 'webcast/im/fetch/', params=params, headers=headers) as res:
                body = await res.read()
                if res.status != 200:
                    logger.warning(
                        'room=%s fetch douyin im state failed, status=%s len=%d',
                        self.room_key, res.status, len(body),
                    )
                    return False

            try:
                response = parse_protobuf(body)
            except ValueError:
                logger.warning('room=%s parse douyin im state failed, len=%d', self.room_key, len(body))
                return False

            self._apply_response_state(response)
            if self._cursor == '':
                logger.warning('room=%s douyin im state missing cursor', self.room_key)
                return False

            logger.info('room=%s fetched douyin initial im state', self.room_key)
            return True
        except Exception:
            logger.warning('room=%s fetch douyin im state failed', self.room_key, exc_info=True)
            return False

    def build_websocket_url(self) -> str:
        now_ms = int(time.time() * 1000)
        signature = self._signer.sign(self._room_id, self._user_unique_id)
        if self._cursor == '':
            # im/fetch 拿不到时用本地生成的游标兜底
            self._cursor = 't-%d_r-%d_d-1_u-1_h-%d' % (
                now_ms, random.randint(10 ** 18, 10 ** 19 - 1), random.randint(10 ** 18, 10 ** 19 - 1)
            )
        if self._internal_ext == '':
            self._internal_ext = (
                'internal_src:dim|wss_push_room_id:%s|wss_push_did:%s|first_req_ms:%d|fetch_time:%d'
                '|seq:1|wss_info:0-%d-0-0|wrds_v:%d'
            ) % (self._room_id, self._user_unique_id, now_ms, now_ms, now_ms,
                 random.randint(10 ** 18, 10 ** 19 - 1))
        push_url = self._push_url or WEBCAST_PUSH_URL

        def encode(value):
            return str(value).replace(' ', '%20')

        query = '&'.join([
            'app_name=' + WEBCAST_APP_NAME,
            'version_code=' + WEBCAST_VERSION_CODE,
            'webcast_sdk_version=' + WEBCAST_SDK_VERSION,
            'update_version_code=' + WEBCAST_SDK_VERSION,
            'compress=gzip',
            'device_platform=web',
            'cookie_enabled=true',
            'screen_width=1920',
            'screen_height=1080',
            'browser_language=zh-CN',
            'browser_platform=Win32',
            'browser_name=Mozilla',
            'browser_version=' + encode(USER_AGENT.split('Mozilla/', 1)[-1]),
            'browser_online=true',
            'tz_name=Asia/Shanghai',
            'cursor=' + encode(self._cursor),
            'internal_ext=' + encode(self._internal_ext),
            'host=' + WEBCAST_HOST,
            'aid=' + WEBCAST_AID,
            'live_id=' + WEBCAST_LIVE_ID,
            'did_rule=' + WEBCAST_DID_RULE,
            'endpoint=live_pc',
            'support_wrds=1',
            'user_unique_id=' + encode(self._user_unique_id),
            'im_path=/webcast/im/fetch/',
            'identity=audience',
            'need_persist_msg_count=15',
            'insert_task_id=',
            'live_reason=',
            'room_id=' + encode(self._room_id),
            'heartbeatDuration=0',
            'signature=' + encode(signature),
        ])
        return push_url + '?' + query

    async def _run_websocket(self):
        url = self.build_websocket_url()
        headers = {
            'User-Agent': USER_AGENT,
            'Origin': WEBCAST_HOST,
            'Referer': REFERER,
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        if self._cookie_header != '':
            headers['Cookie'] = self._cookie_header
        logger.info('room=%s connecting to douyin, room_id=%s', self.room_key, self._room_id)
        async with self._session.ws_connect(
            url, headers=headers, max_msg_size=MAX_MESSAGE_SIZE, heartbeat=10
        ) as websocket:
            logger.info('room=%s connected to douyin', self.room_key)
            await websocket.send_bytes(make_heartbeat_frame())
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket))
            loop = asyncio.get_running_loop()
            last_business_time = loop.time()
            try:
                while True:
                    idle_time = loop.time() - last_business_time
                    if idle_time >= BUSINESS_TIMEOUT:
                        logger.warning(
                            'room=%s douyin websocket no business messages for %.0fs, reconnecting',
                            self.room_key, idle_time,
                        )
                        return

                    timeout = min(READ_TIMEOUT, BUSINESS_TIMEOUT - idle_time)
                    message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
                    if message.type == aiohttp.WSMsgType.BINARY:
                        if await self._handle_push_frame(websocket, message.data):
                            last_business_time = loop.time()
                    elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        logger.info('room=%s douyin websocket closed, type=%s', self.room_key, message.type)
                        return
            except asyncio.TimeoutError:
                idle_time = loop.time() - last_business_time
                if idle_time >= BUSINESS_TIMEOUT:
                    logger.warning(
                        'room=%s douyin websocket no business messages for %.0fs, reconnecting',
                        self.room_key, idle_time,
                    )
                else:
                    logger.info('room=%s douyin websocket read timeout', self.room_key)
            finally:
                heartbeat_task.cancel()

    async def _heartbeat_loop(self, websocket):
        try:
            while True:
                await asyncio.sleep(10)
                await asyncio.wait_for(websocket.send_bytes(make_heartbeat_frame()), timeout=WRITE_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('room=%s douyin heartbeat failed', self.room_key)

    async def _handle_push_frame(self, websocket, data: bytes) -> bool:
        try:
            frame = parse_protobuf(data)
        except ValueError:
            logger.warning('room=%s parse push frame failed, len=%d', self.room_key, len(data))
            return False

        payload_type = pb_str(frame, 7)
        if payload_type not in ('msg', 'gz'):
            return False

        payload = pb_bytes(frame, 8)
        if payload[:2] == b'\x1f\x8b':
            # 上游声明compress=gzip，protobuf载荷是gzip压缩的
            try:
                payload = gzip.decompress(payload)
            except OSError:
                logger.warning('room=%s decompress push frame failed', self.room_key)
                return False

        try:
            response = parse_protobuf(payload)
        except ValueError:
            logger.warning('room=%s parse response failed, len=%d', self.room_key, len(payload))
            return False

        self._apply_response_state(response)
        await self._send_ack_if_needed(websocket, frame, response)

        for message_fields in pb_messages(response, 1):
            method = pb_str(message_fields, 1)
            message = decode_im_message(method, pb_bytes(message_fields, 2))
            if message is None:
                continue
            self._remember_douyin_user(message)
            if method == 'WebcastControlMessage' and message.get('action') == CONTROL_ACTION_LIVE_END:
                self.send_live_status(STATUS_ENDED, '抖音直播间已下播，等待重新开播', False)
                raise _LiveEnded()
            if self._handler is not None:
                try:
                    self._handler.handle(self, message)
                except Exception:
                    logger.exception('room=%s handle douyin message failed, method=%s', self.room_key, method)
        return True

    def _remember_douyin_user(self, message: dict):
        """缓存用户资料；轻礼物没有昵称时用发送者 id 回填"""
        user = message.get('user')
        if isinstance(user, dict) and user.get('nickname'):
            self._user_cache[str(user.get('id'))] = user
            if len(self._user_cache) > 2000:
                self._user_cache.pop(next(iter(self._user_cache)))
            return
        sender_id = str(message.get('senderId') or '')
        if sender_id in ('', '0'):
            return
        cached = self._user_cache.get(sender_id)
        if cached is not None:
            message['user'] = cached

    def _apply_response_state(self, response: dict):
        if pb_str(response, 2) != '':
            self._cursor = pb_str(response, 2)
        if pb_str(response, 5) != '':
            self._internal_ext = pb_str(response, 5)
        push_url = pb_str(response, 14) or pb_str(response, 10)
        if push_url != '':
            self._push_url = _normalize_push_url(push_url)

    async def _send_ack_if_needed(self, websocket, frame: dict, response: dict):
        internal_ext = ''
        for header in pb_messages(frame, 5):
            if pb_str(header, 1) == 'im-internal_ext':
                internal_ext = pb_str(header, 2)
                break
        if internal_ext == '' and pb_int(response, 9) != 0:
            internal_ext = pb_str(response, 5)
        if internal_ext == '':
            return

        try:
            await asyncio.wait_for(
                websocket.send_bytes(make_ack_frame(pb_int(frame, 2), internal_ext)), timeout=WRITE_TIMEOUT
            )
        except Exception:
            logger.warning('room=%s send ack failed', self.room_key)

    def send_live_status(self, code: str, text: str, live: bool):
        """给客户端发送直播状态，状态没变就不重复发"""
        if code == self.last_status_code:
            return
        self.last_status_code = code
        logger.info('room=%s douyin status=%s', self.room_key, code)

        if self._handler is None:
            return
        self._handler.handle(self, {
            'type': 'system',
            'event': 'live_status',
            'code': code,
            'valid': code != STATUS_NOT_FOUND,
            'live': live,
            'status': code.removeprefix('ROOM_').lower(),
            'status_text': text,
            'message': text,
            'room_id': self.room_key.value,
            'live_name': self._live_name,
            'title': self._title,
            'avatar_thumb': self._avatar_thumb,
        })


def _normalize_push_url(candidate: str) -> str:
    """参考 websocket_params.go 的 normalizeWebsocketPushURL"""
    candidate = candidate.split(',')[0].split(';')[0].strip().rstrip('/')
    if candidate == '':
        return ''
    if candidate.startswith('wss://') or candidate.startswith('ws://'):
        pass
    elif candidate.startswith('https://'):
        candidate = 'wss://' + candidate[len('https://'):]
    elif candidate.startswith('http://'):
        candidate = 'ws://' + candidate[len('http://'):]
    else:
        candidate = 'wss://' + candidate
    if '/webcast/im/push/v2' in candidate:
        return candidate + '/'
    return candidate + '/webcast/im/push/v2/'
