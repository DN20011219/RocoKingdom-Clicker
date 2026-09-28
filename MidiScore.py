"""
MIDI 曲谱解析 + 「音高 → 键盘按键」可演奏性分析（纯标准库实现）。

本模块解决三件事：
1. 把标准 MIDI 文件（SMF, format 0/1/2）解析成带秒级时间轴的音符列表；
2. 用一套「音高绑定到哪个键盘按键」的键位表检查曲谱能不能完整演奏，
   不能时精确指出是哪些音没有绑定；
3. 把能演奏的音符编译成按下/抬起事件序列，交给 MusicPlayer 按时序注入。

不引入 mido / python-midi 等第三方库：本项目其余模块（InterceptionCore、
ActionScript、PathPlanner）同样是零第三方依赖，发布包只需 interception.dll，
开发环境只需 Python 3.10。SMF 格式本身足够简单，自己解析可以避免给
PyInstaller 打包和用户环境增加安装负担。
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger("midi_score")


class MidiParseError(Exception):
    """MIDI 文件损坏或不是标准 MIDI 文件。"""


# ──────────────────────────────────────────────
# 音名 / 按键名
# ──────────────────────────────────────────────

_PITCH_CLASSES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# 解析音名时接受的写法：C4 / c4 / C#4 / C♯4 / Db4 / D♭4 / C-1 / 60
# 注意升降号里不接受 ASCII "-"：负八度音名（MIDI 0 = C-1）会和降号冲突，
# "C-1" 必须解成 C 减一个八度而不是 C 降半音的 1 八度。
_NOTE_NAME_RE = re.compile(
    r"^\s*([A-Ga-g])\s*(#|♯|\+|b|♭)?\s*(-?\d{1,2})\s*$"
)
_SHARPS = {"#", "♯", "+"}
_FLATS = {"b", "♭"}


def note_name(pitch: int) -> str:
    """MIDI 音高 → 音名（middle C = 60 = C4）。"""
    if not 0 <= pitch <= 127:
        return f"?{pitch}"
    return f"{_PITCH_CLASSES[pitch % 12]}{pitch // 12 - 1}"


def parse_note_name(text: str) -> Optional[int]:
    """音名 → MIDI 音高；纯数字按 MIDI 音高处理；无法识别返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{1,3}", raw):
        value = int(raw)
        return value if 0 <= value <= 127 else None

    match = _NOTE_NAME_RE.match(raw)
    if not match:
        return None
    letter, accidental, octave = match.group(1).upper(), match.group(2), int(match.group(3))
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter]
    if accidental in _SHARPS:
        base += 1
    elif accidental in _FLATS:
        base -= 1
    pitch = (octave + 1) * 12 + base
    return pitch if 0 <= pitch <= 127 else None


# 可绑定按键 → 虚拟键码。只收录 Interception 能用扫描码注入的键（见
# ActionScript.VK_TO_SCANCODE），避免绑上一个注入不了、只能静默失败的按键。
KEY_VK: dict[str, int] = {}
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    KEY_VK[_ch] = ord(_ch)
for _digit in "0123456789":
    KEY_VK[_digit] = ord(_digit)
for _i in range(1, 13):
    KEY_VK[f"F{_i}"] = 0x6F + _i
KEY_VK.update({
    "SPACE": 0x20,
    "`": 0xC0, "-": 0xBD, "=": 0xBB,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "NUM0": 0x60, "NUM1": 0x61, "NUM2": 0x62, "NUM3": 0x63, "NUM4": 0x64,
    "NUM5": 0x65, "NUM6": 0x66, "NUM7": 0x67, "NUM8": 0x68, "NUM9": 0x69,
})

# 展示用别名：让用户输入更宽容
_KEY_ALIASES = {
    "空格": "SPACE", "SP": "SPACE", "SPACEBAR": "SPACE",
    "ESC": "ESCAPE", "回车": "ENTER",
}


def normalize_key_label(label: str) -> Optional[str]:
    """把用户输入的按键名规范成 KEY_VK 里的键；无法识别返回 None。"""
    raw = str(label or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    upper = _KEY_ALIASES.get(upper, upper)
    if upper in KEY_VK:
        return upper
    # 单字符直接按字面匹配（标点区分大小写无关）
    if len(raw) == 1 and raw.upper() in KEY_VK:
        return raw.upper()
    if len(raw) == 1 and raw in KEY_VK:
        return raw
    return None


def key_to_vk(label: str) -> Optional[int]:
    """按键名 → 虚拟键码；无法识别返回 None。"""
    normalized = normalize_key_label(label)
    return KEY_VK.get(normalized) if normalized else None


# ──────────────────────────────────────────────
# 键位绑定
# ──────────────────────────────────────────────

# 《洛克王国：世界》手碟的九个音（与 RocoMusic 练习页使用的键位一致）。
# 音高采用 MIDI 编号，middle C = 60 = C4。
LAYOUT_ROCO_HANDPAN: tuple[dict, ...] = (
    {"pitch": 45, "key": "B", "note": "低音 6"},
    {"pitch": 52, "key": "F", "note": "3"},
    {"pitch": 53, "key": "G", "note": "4"},
    {"pitch": 55, "key": "H", "note": "5"},
    {"pitch": 57, "key": "J", "note": "6"},
    {"pitch": 59, "key": "K", "note": "7"},
    {"pitch": 60, "key": "T", "note": "高音 1"},
    {"pitch": 62, "key": "Y", "note": "高音 2"},
    {"pitch": 64, "key": "U", "note": "高音 3"},
)

LAYOUT_NAME_ROCO_HANDPAN = "roco_handpan"


@dataclass(frozen=True)
class KeyBinding:
    """一条「音高 → 按键」绑定。"""

    pitch: int
    key: str
    note: str = ""

    @property
    def name(self) -> str:
        return note_name(self.pitch)

    def to_dict(self) -> dict:
        data = {"pitch": int(self.pitch), "key": self.key}
        if self.note:
            data["note"] = self.note
        return data


class KeyLayout:
    """一套键位绑定表。"""

    def __init__(self, bindings: Iterable[KeyBinding] = (), name: str = "custom"):
        self.name = name
        self._bindings: list[KeyBinding] = []
        self._pitch_to_key: dict[int, str] = {}
        self._key_to_pitch: dict[str, int] = {}
        for binding in bindings:
            self.add(binding)

    @classmethod
    def default(cls) -> "KeyLayout":
        """洛克手碟九键（出厂键位）。"""
        return cls(
            (KeyBinding(**item) for item in LAYOUT_ROCO_HANDPAN),
            name=LAYOUT_NAME_ROCO_HANDPAN,
        )

    def add(self, binding: KeyBinding) -> None:
        """加入一条绑定；同一音高重复时后者覆盖前者。"""
        if not 0 <= binding.pitch <= 127:
            raise ValueError(f"音高超出 0~127: {binding.pitch}")
        key = normalize_key_label(binding.key)
        if key is None:
            raise ValueError(f"无法识别的按键名: {binding.key!r}")
        self._bindings = [b for b in self._bindings if b.pitch != binding.pitch]
        self._bindings.append(KeyBinding(binding.pitch, key, binding.note))
        self._bindings.sort(key=lambda b: b.pitch)
        self._rebuild_index()

    def remove(self, pitch: int) -> bool:
        before = len(self._bindings)
        self._bindings = [b for b in self._bindings if b.pitch != pitch]
        if len(self._bindings) == before:
            return False
        self._rebuild_index()
        return True

    def _rebuild_index(self) -> None:
        self._pitch_to_key = {b.pitch: b.key for b in self._bindings}
        # 同一个按键被绑到多个音高时，只保留音高最低的那条，
        # 冲突本身由 conflicts() 报给上层提示用户。
        self._key_to_pitch = {}
        for binding in self._bindings:
            self._key_to_pitch.setdefault(binding.key, binding.pitch)

    @property
    def bindings(self) -> list[KeyBinding]:
        return list(self._bindings)

    @property
    def pitches(self) -> list[int]:
        return [b.pitch for b in self._bindings]

    def __len__(self) -> int:
        return len(self._bindings)

    def has(self, pitch: int) -> bool:
        return pitch in self._pitch_to_key

    def key_for(self, pitch: int) -> Optional[str]:
        return self._pitch_to_key.get(pitch)

    def vk_for(self, pitch: int) -> Optional[int]:
        key = self._pitch_to_key.get(pitch)
        return KEY_VK.get(key) if key else None

    def nearest_pitch(self, pitch: int) -> Optional[int]:
        """返回距离最近的已绑定音高（空表返回 None）。"""
        if not self._bindings:
            return None
        return min(self._bindings, key=lambda b: (abs(b.pitch - pitch), b.pitch)).pitch

    def range_text(self) -> str:
        if not self._bindings:
            return "未绑定任何按键"
        low, high = self._bindings[0], self._bindings[-1]
        return f"{low.name} ~ {high.name}（{len(self._bindings)} 键）"

    def conflicts(self) -> list[str]:
        """返回被重复使用的按键列表。"""
        seen: dict[str, int] = {}
        dupes: list[str] = []
        for binding in self._bindings:
            if binding.key in seen and binding.key not in dupes:
                dupes.append(binding.key)
            seen[binding.key] = binding.pitch
        return dupes

    def to_list(self) -> list[dict]:
        return [b.to_dict() for b in self._bindings]

    @classmethod
    def from_list(cls, data: Sequence[dict], name: str = "custom") -> "KeyLayout":
        """从配置里的列表重建键位表，跳过非法条目而不是整体失败。"""
        layout = cls(name=name)
        for item in data or ():
            if not isinstance(item, dict):
                continue
            try:
                pitch = int(item.get("pitch"))
            except (TypeError, ValueError):
                logger.warning("键位绑定缺少合法音高，已跳过: %s", item)
                continue
            key = normalize_key_label(str(item.get("key", "")))
            if key is None:
                logger.warning("键位绑定按键名无法识别，已跳过: %s", item)
                continue
            if not 0 <= pitch <= 127:
                logger.warning("键位绑定音高越界，已跳过: %s", item)
                continue
            layout.add(KeyBinding(pitch, key, str(item.get("note") or "")))
        return layout


# ──────────────────────────────────────────────
# MIDI 解析
# ──────────────────────────────────────────────

@dataclass
class MidiNote:
    """一个音符。start / duration 单位为秒。"""

    pitch: int
    start: float
    duration: float
    velocity: int = 100
    track: int = 0
    channel: int = 0
    track_name: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def name(self) -> str:
        return note_name(self.pitch)


@dataclass
class MidiTrackInfo:
    index: int
    name: str
    channel: int
    note_count: int


@dataclass
class MidiScore:
    """解析后的整首曲谱。"""

    path: Path
    name: str
    notes: list[MidiNote] = field(default_factory=list)
    tracks: list[MidiTrackInfo] = field(default_factory=list)
    duration: float = 0.0
    bpm: float = 120.0
    ppq: int = 480
    midi_format: int = 1
    tempo_changes: int = 1

    @property
    def pitch_range(self) -> tuple[Optional[int], Optional[int]]:
        if not self.notes:
            return None, None
        pitches = [n.pitch for n in self.notes]
        return min(pitches), max(pitches)

    def duration_text(self) -> str:
        total = int(round(self.duration))
        return f"{total // 60}:{total % 60:02d}"


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读取 MIDI 变长数值，返回 (value, 新位置)。"""
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise MidiParseError("变长数值在文件末尾被截断")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise MidiParseError("变长数值超过 4 字节，文件可能已损坏")


def _iter_chunks(data: bytes):
    """遍历 RIFF 风格的 chunk，产出 (chunk_id, body)。"""
    pos = 0
    total = len(data)
    while pos + 8 <= total:
        chunk_id = data[pos:pos + 4]
        length = int.from_bytes(data[pos + 4:pos + 8], "big")
        body_start = pos + 8
        body_end = body_start + length
        if body_end > total:
            # 尾部被截断的 chunk 仍然解析已有部分，比整体失败更有用
            logger.warning("chunk %s 声明长度 %d 超出文件，按 %d 字节处理",
                           chunk_id, length, total - body_start)
            body_end = total
        yield chunk_id, data[body_start:body_end]
        pos = body_start + length


# 轨道事件类型（解析后的中间表示）
_EV_NOTE_ON = 0
_EV_NOTE_OFF = 1
_EV_ALL_NOTES_OFF = 2


def _decode_text(payload: bytes) -> str:
    """解码 MIDI 里的文本元事件，解不出可读文本时返回空串。

    规范上应该是 ASCII/UTF-8，但国产制谱软件普遍写 GBK，所以按 UTF-8 → GBK →
    Big5 依次尝试。这里故意不做 latin-1 兜底：部分导出器写进来的轨道名其实是
    二进制垃圾（本仓库示例曲谱 callofslience.mid 就是），latin-1 永远"解码成功"，
    只会把垃圾变成一串看着像西欧文字的乱码显示给用户。宁可返回空串让上层退回
    "轨道 N" 这种诚实的显示。
    """
    for encoding in ("utf-8", "gbk", "big5"):
        try:
            text = payload.decode(encoding).strip()
        except (UnicodeDecodeError, LookupError):
            continue
        # isprintable() 会拒绝 \r \n \t 等控制字符，正好过滤掉半二进制垃圾
        if text and text.isprintable():
            return text
    return ""


def _parse_track_body(body: bytes) -> tuple[list, list, str, int]:
    """解析一条 MTrk，返回 (事件列表, 速度事件列表, 轨道名, 轨道结束 tick)。

    事件为 (abs_tick, kind, channel, pitch, velocity)。
    轨道结束 tick 取解析停止处的绝对 tick（通常是 end-of-track 元事件的位置），
    用来给缺少 note off 的悬挂音符补一个合理的时长。
    """
    events: list[tuple] = []
    tempos: list[tuple[int, int]] = []
    track_name = ""
    pos = 0
    total = len(body)
    abs_tick = 0
    running_status = 0

    while pos < total:
        delta, pos = _read_varint(body, pos)
        abs_tick += delta
        if pos >= total:
            break
        status = body[pos]

        if status < 0x80:
            # 数据字节：沿用 running status
            if not running_status:
                raise MidiParseError(f"轨道事件缺少状态字节（tick={abs_tick}）")
            status = running_status
        else:
            pos += 1
            if status < 0xF0:
                running_status = status

        high = status & 0xF0
        channel = status & 0x0F

        if status == 0xFF:
            if pos >= total:
                break
            meta_type = body[pos]
            pos += 1
            length, pos = _read_varint(body, pos)
            payload = body[pos:pos + length]
            pos += length
            if meta_type == 0x51 and len(payload) >= 3:
                tempo = int.from_bytes(payload[:3], "big")
                if tempo > 0:
                    tempos.append((abs_tick, tempo))
            elif meta_type == 0x03 and not track_name:
                track_name = _decode_text(payload)
            elif meta_type == 0x2F:
                break
            continue

        if status in (0xF0, 0xF7):
            length, pos = _read_varint(body, pos)
            pos += length
            continue

        if high in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
            if pos + 1 >= total:
                break
            first, second = body[pos], body[pos + 1]
            pos += 2
        elif high in (0xC0, 0xD0):
            if pos >= total:
                break
            first = body[pos]
            second = 0
            pos += 1
        else:
            continue

        if high == 0x90 and second > 0:
            events.append((abs_tick, _EV_NOTE_ON, channel, first, second))
        elif high == 0x80 or (high == 0x90 and second == 0):
            events.append((abs_tick, _EV_NOTE_OFF, channel, first, 0))
        elif high == 0xB0 and first in (120, 123, 124, 125, 126, 127):
            # All Sound Off / All Notes Off 一类的通道模式消息
            events.append((abs_tick, _EV_ALL_NOTES_OFF, channel, 0, 0))

    return events, tempos, track_name, abs_tick


def _build_tick_converter(ppq: int, tempos: Sequence[tuple[int, int]]):
    """构造 tick → 秒 的分段线性换算函数。"""
    ordered = sorted(set(tempos)) if tempos else [(0, 500_000)]
    if ordered[0][0] != 0:
        ordered.insert(0, (0, ordered[0][1]))

    # 每个速度段的起点：(tick, 该 tick 对应的秒, 每 tick 多少秒)
    anchors: list[tuple[int, float, float]] = []
    for tick, tempo in ordered:
        seconds = 0.0
        if anchors:
            prev_tick, prev_sec, prev_rate = anchors[-1]
            seconds = prev_sec + (tick - prev_tick) * prev_rate
        anchors.append((tick, seconds, tempo / ppq / 1_000_000.0))

    def to_seconds(tick: int) -> float:
        lo = 0
        for index in range(1, len(anchors)):
            if anchors[index][0] <= tick:
                lo = index
            else:
                break
        anchor_tick, anchor_sec, rate = anchors[lo]
        return anchor_sec + (tick - anchor_tick) * rate

    return to_seconds


def parse_midi(path: str | Path) -> MidiScore:
    """解析一个标准 MIDI 文件。

    Raises:
        MidiParseError: 文件不是 MIDI、为空或结构损坏。
        FileNotFoundError: 文件不存在。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"MIDI 文件不存在: {path}")
    data = path.read_bytes()
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiParseError(f"不是标准 MIDI 文件（缺少 MThd 头）: {path.name}")

    chunks = list(_iter_chunks(data))
    header = next((body for cid, body in chunks if cid == b"MThd"), None)
    if header is None or len(header) < 6:
        raise MidiParseError("MIDI 头块长度不足")

    midi_format = int.from_bytes(header[0:2], "big")
    declared_tracks = int.from_bytes(header[2:4], "big")
    division = int.from_bytes(header[4:6], "big")
    if division == 0:
        raise MidiParseError("MIDI 头块 division 为 0")

    if division & 0x8000:
        # SMPTE 时基：每帧 tick 数 × 帧率，换算成「每秒多少 tick」后按 1 秒 = 1 拍处理
        frames_per_sec = -((division >> 8) - 256)
        ticks_per_frame = division & 0xFF
        if frames_per_sec <= 0 or ticks_per_frame <= 0:
            raise MidiParseError(f"无法识别的 SMPTE 时基: 0x{division:04x}")
        ppq = frames_per_sec * ticks_per_frame
        tempo_scale = 1_000_000  # 每拍 1 秒
    else:
        ppq = division
        tempo_scale = 0

    track_bodies = [body for cid, body in chunks if cid == b"MTrk"]
    if not track_bodies:
        raise MidiParseError("MIDI 文件没有任何音轨")
    if declared_tracks and len(track_bodies) != declared_tracks:
        logger.warning("%s: 声明 %d 轨，实际 %d 轨", path.name,
                       declared_tracks, len(track_bodies))

    all_tempos: list[tuple[int, int]] = []
    parsed_tracks: list[tuple[list, str, int]] = []
    for body in track_bodies:
        events, tempos, track_name, end_tick = _parse_track_body(body)
        all_tempos.extend(tempos)
        parsed_tracks.append((events, track_name, end_tick))

    if tempo_scale:
        all_tempos = [(tick, tempo_scale) for tick, _ in all_tempos] or [(0, tempo_scale)]
    to_seconds = _build_tick_converter(ppq, all_tempos)

    notes: list[MidiNote] = []
    track_infos: list[MidiTrackInfo] = []
    for track_index, (events, track_name, track_end_tick) in enumerate(parsed_tracks):
        # (channel, pitch) → 未闭合的 (起始 tick, 力度) 队列；
        # 同一音高连击时按先来先配对
        pending: dict[tuple[int, int], list[tuple[int, int]]] = {}
        channels: set[int] = set()
        track_note_count = 0
        last_tick = 0

        def close(channel: int, pitch: int, tick: int, velocity: int) -> None:
            nonlocal track_note_count
            starts = pending.get((channel, pitch))
            if not starts:
                return
            start_tick, _ = starts.pop(0)
            duration = to_seconds(tick) - to_seconds(start_tick)
            if duration <= 0:
                # 同 tick 内的按下+抬起：给一个最小可闻时长，避免事件序列出现负区间
                duration = 0.02
            notes.append(MidiNote(
                pitch=pitch,
                start=to_seconds(start_tick),
                duration=duration,
                velocity=velocity,
                track=track_index,
                channel=channel,
                track_name=track_name,
            ))
            track_note_count += 1

        for abs_tick, kind, channel, pitch, velocity in events:
            last_tick = max(last_tick, abs_tick)
            if kind == _EV_NOTE_ON:
                pending.setdefault((channel, pitch), []).append((abs_tick, velocity))
                channels.add(channel)
            elif kind == _EV_NOTE_OFF:
                starts = pending.get((channel, pitch))
                start_velocity = starts[0][1] if starts else 100
                close(channel, pitch, abs_tick, start_velocity)
                channels.add(channel)
            elif kind == _EV_ALL_NOTES_OFF:
                for (ch, pit) in [k for k in pending if k[0] == channel and pending[k]]:
                    while pending.get((ch, pit)):
                        close(ch, pit, abs_tick, pending[(ch, pit)][0][1])

        # 没有对应 note off 的悬挂音符：补到轨道末尾，否则这些音会被静默丢掉
        for (channel, pitch), starts in pending.items():
            for start_tick, velocity in starts:
                end_tick = max(last_tick, track_end_tick, start_tick + 1)
                close(channel, pitch, end_tick, velocity)
                logger.warning("%s 轨道 %d: 音高 %d 缺少 note off，已补到轨道末尾",
                               path.name, track_index, pitch)

        track_infos.append(MidiTrackInfo(
            index=track_index,
            name=track_name or f"轨道 {track_index + 1}",
            channel=min(channels) if len(channels) == 1 else -1,
            note_count=track_note_count,
        ))

    notes.sort(key=lambda n: (n.start, n.pitch))
    duration = max((n.end for n in notes), default=0.0)

    # 用「第一个速度」作为展示用 BPM；曲谱内部仍按完整速度表换算时间
    first_tempo = min(all_tempos, key=lambda t: t[0])[1] if all_tempos else 500_000
    bpm = 60_000_000.0 / first_tempo if first_tempo else 120.0

    score = MidiScore(
        path=path,
        name=path.stem,
        notes=notes,
        tracks=track_infos,
        duration=duration,
        bpm=bpm,
        ppq=ppq,
        midi_format=midi_format,
        tempo_changes=max(1, len(set(all_tempos))),
    )
    logger.info("解析 %s: %d 轨 / %d 音符 / %s / %.1f BPM",
                path.name, len(track_infos), len(notes),
                score.duration_text(), bpm)
    return score


# ──────────────────────────────────────────────
# 可演奏性检查
# ──────────────────────────────────────────────

@dataclass
class MissingNote:
    """一个没有绑定按键的音高及其在曲谱中的出现情况。"""

    pitch: int
    count: int
    first_time: float
    nearest_pitch: Optional[int] = None
    nearest_semitones: int = 0

    @property
    def name(self) -> str:
        return note_name(self.pitch)

    @property
    def nearest_name(self) -> str:
        return note_name(self.nearest_pitch) if self.nearest_pitch is not None else "—"

    def describe(self) -> str:
        """一行人类可读的说明。"""
        text = (f"{self.name}（MIDI {self.pitch}）出现 {self.count} 次，"
                f"首次 {self.first_time:.2f}s")
        if self.nearest_pitch is not None and self.nearest_semitones:
            direction = "高" if self.nearest_semitones > 0 else "低"
            text += f"；最近的已绑定音是 {self.nearest_name}（{direction} {abs(self.nearest_semitones)} 个半音）"
        return text


@dataclass
class ScoreCheck:
    """一次可演奏性检查的结果。"""

    ok: bool
    total_notes: int
    playable_notes: int
    missing: list[MissingNote] = field(default_factory=list)
    shift: int = 0
    min_pitch: Optional[int] = None
    max_pitch: Optional[int] = None
    duration: float = 0.0
    bpm: float = 120.0
    peak_notes_per_second: int = 0
    same_key_overlaps: int = 0

    @property
    def missing_notes(self) -> int:
        return self.total_notes - self.playable_notes

    @property
    def missing_pitches(self) -> int:
        return len(self.missing)

    def summary(self) -> str:
        """一句话结论，用于面板顶部的状态行。"""
        low = note_name(self.min_pitch) if self.min_pitch is not None else "—"
        high = note_name(self.max_pitch) if self.max_pitch is not None else "—"
        base = (f"{self.total_notes} 个音符 · 音域 {low}~{high} · "
                f"{int(self.duration) // 60}:{int(self.duration) % 60:02d} · "
                f"{self.bpm:.0f} BPM")
        if self.shift:
            base += f" · 移调 {self.shift:+d} 半音"
        return base

    def describe_missing(self, limit: int = 0) -> str:
        """缺失音的完整清单文本（limit=0 表示不截断）。"""
        if not self.missing:
            return ""
        items = self.missing if limit <= 0 else self.missing[:limit]
        lines = [f"以下 {len(self.missing)} 个音没有绑定按键："]
        lines.extend(f"  · {item.describe()}" for item in items)
        if limit > 0 and len(self.missing) > limit:
            lines.append(f"  …另有 {len(self.missing) - limit} 个音未列出")
        lines.append(
            f"合计 {self.missing_notes} 个音符无法演奏"
            f"（占 {self.missing_notes * 100.0 / max(1, self.total_notes):.1f}%）。"
        )
        return "\n".join(lines)


def _shifted_pitch(pitch: int, shift: int) -> int:
    return max(0, min(127, pitch + shift))


def check_score(score: MidiScore, layout: KeyLayout, shift: int = 0) -> ScoreCheck:
    """检查曲谱在当前键位表（可选移调）下能否完整演奏。

    判定标准很直接：曲谱里出现的每个音高，移调后都必须能在键位表里找到按键。
    只要有一个音找不到，就返回 ok=False 并列出全部缺失音，供 GUI 精确提示。
    """
    if not score.notes:
        return ScoreCheck(ok=False, total_notes=0, playable_notes=0, shift=shift)

    missing_map: dict[int, MissingNote] = {}
    playable = 0
    onsets: list[float] = []
    key_spans: dict[str, list[tuple[float, float]]] = {}
    pitches = [n.pitch for n in score.notes]

    for note in score.notes:
        pitch = _shifted_pitch(note.pitch, shift)
        onsets.append(note.start)
        key = layout.key_for(pitch)
        if key is None:
            entry = missing_map.get(pitch)
            if entry is None:
                nearest = layout.nearest_pitch(pitch)
                missing_map[pitch] = MissingNote(
                    pitch=pitch,
                    count=1,
                    first_time=note.start,
                    nearest_pitch=nearest,
                    nearest_semitones=(pitch - nearest) if nearest is not None else 0,
                )
            else:
                entry.count += 1
            continue
        playable += 1
        key_spans.setdefault(key, []).append((note.start, note.end))

    # 同一按键在 1 秒内被按下的最多次数的：密度过高时游戏里来不及抬手
    peak = 0
    if onsets:
        onsets.sort()
        left = 0
        for right in range(len(onsets)):
            while onsets[right] - onsets[left] >= 1.0:
                left += 1
            peak = max(peak, right - left + 1)

    # 同键时值重叠：同一个按键还没抬起又要按下，演奏时必须重触发才能都发出声
    overlaps = 0
    for spans in key_spans.values():
        spans.sort()
        held_until = spans[0][1]
        for start, end in spans[1:]:
            if start < held_until:
                overlaps += 1
            held_until = max(held_until, end)

    missing = sorted(missing_map.values(), key=lambda m: (-m.count, m.pitch))
    return ScoreCheck(
        ok=not missing and playable > 0,
        total_notes=len(score.notes),
        playable_notes=playable,
        missing=missing,
        shift=shift,
        min_pitch=min(pitches),
        max_pitch=max(pitches),
        duration=score.duration,
        bpm=score.bpm,
        peak_notes_per_second=peak,
        same_key_overlaps=overlaps,
    )


def find_shifts(score: MidiScore, layout: KeyLayout,
                semitones: int = 24) -> list[tuple[int, int, int]]:
    """在 ±semitones 半音内搜索移调方案，返回按优劣排序的候选。

    每项为 (shift, playable, missing_pitches)。排序规则：
    先按可演奏音符数从多到少，再按 |移调量| 从小到大 —— 所以第一项就是
    「能完整演奏且改动最小」的方案；若没有任何方案能完整演奏，第一项是
    丢失音符最少的方案。
    """
    if not score.notes or not len(layout):
        return []

    counts: dict[int, int] = {}
    for note in score.notes:
        counts[note.pitch] = counts.get(note.pitch, 0) + 1
    bound = set(layout.pitches)

    results: list[tuple[int, int, int]] = []
    for shift in range(-semitones, semitones + 1):
        playable_notes = 0
        missing_pitches = 0
        for pitch, count in counts.items():
            if _shifted_pitch(pitch, shift) in bound:
                playable_notes += count
            else:
                missing_pitches += 1
        results.append((shift, playable_notes, missing_pitches))

    results.sort(key=lambda item: (-item[1], abs(item[0])))
    return results


def best_shift(score: MidiScore, layout: KeyLayout, semitones: int = 24) -> Optional[int]:
    """返回能完整演奏整首曲谱的最小移调量；不存在这样的移调时返回 None。"""
    for shift, playable, _missing in find_shifts(score, layout, semitones):
        if playable == len(score.notes):
            return shift
    return None


# ──────────────────────────────────────────────
# 编译成按键事件序列
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class KeyEvent:
    """一次按键动作。time 为相对演奏开始的秒数（未应用速度倍率）。"""

    time: float
    down: bool
    key: str
    vk: int
    pitch: int


@dataclass
class Performance:
    """可直接交给 MusicPlayer 演奏的事件序列。"""

    events: list[KeyEvent] = field(default_factory=list)
    note_count: int = 0
    duration: float = 0.0
    conflicts: int = 0
    skipped: int = 0

    @property
    def key_count(self) -> int:
        return len({e.key for e in self.events})


@dataclass
class PerformanceOptions:
    """编译参数。"""

    hold_ratio: float = 0.9      # 按键保持占音符时值的比例，留出抬手时间
    min_hold_ms: int = 45        # 最短按住时长，太短游戏可能识别不到
    retrigger_gap_ms: int = 12   # 同键两次按下之间的最小间隔
    max_polyphony: int = 0       # 0 = 不限；>0 时同一时刻最多保留这么多音


def build_performance(score: MidiScore, layout: KeyLayout, shift: int = 0,
                      options: PerformanceOptions | None = None) -> Performance:
    """把曲谱编译成按键事件序列。

    核心难点是同一个按键上的音符重叠：物理键盘（和游戏）在一个按键被按住
    期间再按一次不会产生第二个音，所以必须提前抬起再按下（重触发）。
    这里按按键分组处理，保证每个音符都对应一次真实的按下动作。
    """
    opts = options or PerformanceOptions()
    hold_ratio = min(1.0, max(0.05, float(opts.hold_ratio)))
    min_hold = max(0.005, opts.min_hold_ms / 1000.0)
    gap = max(0.001, opts.retrigger_gap_ms / 1000.0)

    notes = []
    skipped = 0
    for note in score.notes:
        pitch = _shifted_pitch(note.pitch, shift)
        key = layout.key_for(pitch)
        vk = KEY_VK.get(key) if key else None
        if key is None or vk is None:
            skipped += 1
            continue
        notes.append((note, pitch, key, vk))

    if opts.max_polyphony and opts.max_polyphony > 0:
        notes = _limit_polyphony(notes, opts.max_polyphony)

    by_key: dict[str, list[tuple]] = {}
    for item in notes:
        by_key.setdefault(item[2], []).append(item)

    events: list[KeyEvent] = []
    conflicts = 0
    for key, group in by_key.items():
        group.sort(key=lambda item: (item[0].start, item[0].pitch))
        for index, (note, pitch, _key, vk) in enumerate(group):
            press = note.start
            release = press + max(min_hold, note.duration * hold_ratio)
            if index + 1 < len(group):
                next_press = group[index + 1][0].start
                if next_press < release:
                    # 下一个同键音符在本次抬起之前就要按下：提前抬手重触发
                    release = min(release, next_press - gap)
                    if release < press + min_hold:
                        conflicts += 1
                        release = max(press + min_hold, next_press - gap)
            events.append(KeyEvent(press, True, key, vk, pitch))
            events.append(KeyEvent(release, False, key, vk, pitch))

    events.sort(key=lambda e: (e.time, e.down))
    duration = max((e.time for e in events), default=0.0)
    if conflicts:
        logger.warning("有 %d 处同键音符间隔过短（< %.0fms），可能少发声",
                       conflicts, opts.retrigger_gap_ms)
    return Performance(
        events=events,
        note_count=len(notes),
        duration=duration,
        conflicts=conflicts,
        skipped=skipped,
    )


def _limit_polyphony(notes: list, max_polyphony: int) -> list:
    """同一时刻音符过多时，保留音高最高（旋律通常在高声部）的几个。"""
    kept: list = []
    active: list[tuple[float, tuple]] = []
    for item in sorted(notes, key=lambda entry: entry[0].start):
        note = item[0]
        active = [entry for entry in active if entry[0] > note.start]
        if len(active) >= max_polyphony:
            # 挤掉当前最低的音，让新的高音进来
            active.sort(key=lambda entry: entry[1][1])
            dropped = active.pop(0)
            kept = [entry for entry in kept if entry is not dropped[1]]
        active.append((note.end, item))
        kept.append(item)
    kept.sort(key=lambda entry: entry[0].start)
    return kept


# ──────────────────────────────────────────────
# 曲谱库（data/music）
# ──────────────────────────────────────────────

class MusicLibrary:
    """管理 data/music 目录下的 MIDI 曲谱。"""

    EXTENSIONS = (".mid", ".midi")

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.music_dir = self.data_dir / "music"

    def ensure_dir(self) -> Path:
        self.music_dir.mkdir(parents=True, exist_ok=True)
        return self.music_dir

    def list_scores(self) -> list[dict]:
        """列出曲谱库里的全部 MIDI，按文件名排序。"""
        if not self.music_dir.exists():
            return []
        items = []
        for path in sorted(self.music_dir.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_file() or path.suffix.lower() not in self.EXTENSIONS:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            items.append({
                "name": path.stem,
                "filename": path.name,
                "size": size,
                "path": str(path),
            })
        return items

    def resolve(self, name: str) -> Optional[Path]:
        """按显示名（不含扩展名）定位曲谱文件。"""
        name = (name or "").strip()
        if not name:
            return None
        for suffix in self.EXTENSIONS:
            candidate = self.music_dir / f"{name}{suffix}"
            if candidate.exists():
                return candidate
        # 允许直接传完整文件名
        direct = self.music_dir / name
        if direct.exists() and direct.suffix.lower() in self.EXTENSIONS:
            return direct
        return None

    def import_file(self, source: str | Path) -> Optional[Path]:
        """把一个 MIDI 文件复制进曲谱库，重名时自动加序号。"""
        source = Path(source)
        if not source.exists() or source.suffix.lower() not in self.EXTENSIONS:
            return None
        self.ensure_dir()
        target = self.music_dir / source.name
        counter = 2
        while target.exists():
            target = self.music_dir / f"{source.stem}_{counter}{source.suffix}"
            counter += 1
        shutil.copy2(source, target)
        logger.info("已导入曲谱: %s -> %s", source, target)
        return target

    def delete(self, name: str) -> bool:
        path = self.resolve(name)
        if path is None:
            return False
        try:
            path.unlink()
            logger.info("已删除曲谱: %s", path)
            return True
        except OSError as exc:
            logger.error("删除曲谱失败 %s: %s", path, exc)
            return False
