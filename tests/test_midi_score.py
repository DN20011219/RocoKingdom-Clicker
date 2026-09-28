"""MidiScore 的单元测试。

运行方式（项目根目录）：

    python -m unittest discover -s tests -v

只用标准库：被测模块本身零第三方依赖，测试也不引入 pytest，这样 CI 和本地都
不需要额外安装东西。MIDI 解析不能只靠仓库里的示例曲谱来测（它只覆盖一种
情况，而且用户可能删掉它），所以这里用 _track_from_notes 现场合成 SMF 字节流，
按需构造重叠音符、变速、running status、悬挂音符等各种边界情况。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MidiScore import (  # noqa: E402
    KeyBinding,
    KeyLayout,
    MidiParseError,
    MusicLibrary,
    PerformanceOptions,
    best_shift,
    build_performance,
    check_score,
    find_shifts,
    key_to_vk,
    note_name,
    normalize_key_label,
    parse_midi,
    parse_note_name,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_MIDI = PROJECT_ROOT / "data" / "music" / "callofslience.mid"

PPQ = 480


# ──────────────────────────────────────────────
# 合成 MIDI 的小工具
# ──────────────────────────────────────────────

def _varint(value: int) -> bytes:
    """MIDI 变长数值编码。"""
    if value < 0:
        raise ValueError(value)
    chunks = [value & 0x7F]
    value >>= 7
    while value:
        chunks.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(chunks))


def _note_on(delta: int, channel: int, pitch: int, velocity: int = 100) -> bytes:
    return _varint(delta) + bytes([0x90 | channel, pitch, velocity])


def _note_off(delta: int, channel: int, pitch: int) -> bytes:
    return _varint(delta) + bytes([0x80 | channel, pitch, 0])


def _tempo(delta: int, bpm: float) -> bytes:
    microseconds = round(60_000_000 / bpm)
    return _varint(delta) + b"\xff\x51\x03" + microseconds.to_bytes(3, "big")


def _track_name(delta: int, text: str, encoding: str = "utf-8") -> bytes:
    payload = text.encode(encoding)
    return _varint(delta) + b"\xff\x03" + _varint(len(payload)) + payload


def _end_of_track(delta: int = 0) -> bytes:
    return _varint(delta) + b"\xff\x2f\x00"


def _build_midi(tracks: list[bytes], ppq: int = PPQ, midi_format: int = 1) -> bytes:
    out = bytearray()
    out += b"MThd" + (6).to_bytes(4, "big")
    out += midi_format.to_bytes(2, "big")
    out += len(tracks).to_bytes(2, "big")
    out += ppq.to_bytes(2, "big")
    for body in tracks:
        out += b"MTrk" + len(body).to_bytes(4, "big") + body
    return bytes(out)


def _write_tmp(data: bytes, name: str = "synthetic.mid") -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / name
    path.write_bytes(data)
    return tmp, path


def _track_from_notes(notes, *, bpm: float = 120.0, name: str | None = None,
                      channel_default: int = 0) -> bytes:
    """按「秒」描述的音符列表生成一条 MTrk。

    notes 每项为 (start, duration, pitch) 或 (start, duration, pitch, channel)。
    先算出绝对 tick 再排序做 delta 编码，因此可以表达任意重叠/错位，
    比手写一串 delta 直观得多。
    """
    ticks_per_second = PPQ / (60.0 / bpm)
    raw: list[tuple[int, int, bytes]] = []
    for spec in notes:
        start, duration, pitch = spec[0], spec[1], spec[2]
        channel = spec[3] if len(spec) > 3 else channel_default
        on_tick = int(round(start * ticks_per_second))
        off_tick = int(round((start + duration) * ticks_per_second))
        # 同一 tick 上先处理 note off 再处理 note on，否则「上一个音结束的
        # 瞬间下一个同音高音开始」会被解析器错配成一对
        raw.append((off_tick, 0, bytes([0x80 | channel, pitch, 0])))
        raw.append((on_tick, 1, bytes([0x90 | channel, pitch, 100])))
    raw.sort(key=lambda item: (item[0], item[1]))

    body = bytearray(_tempo(0, bpm))
    if name:
        body += _track_name(0, name)
    previous = 0
    for tick, _order, payload in raw:
        body += _varint(tick - previous) + payload
        previous = tick
    body += _end_of_track()
    return bytes(body)


def _score_from_notes(notes, *, bpm: float = 120.0, name: str | None = None,
                      midi_format: int = 1):
    """合成一份只有一个音符轨的曲谱并解析出来。调用方负责关闭临时目录。"""
    track = _track_from_notes(notes, bpm=bpm, name=name)
    tmp, path = _write_tmp(_build_midi([track], midi_format=midi_format))
    return tmp, parse_midi(path)


def _score_with_pitches(pitches, *, duration: float = 0.5, spacing: float | None = None):
    """把音高列表排成一条旋律：每个音时长 duration 秒，起点间隔 spacing 秒。

    spacing < duration 时相邻音符互相重叠，用来构造同键重触发场景。
    """
    if spacing is None:
        spacing = duration
    notes = [(index * spacing, duration, pitch) for index, pitch in enumerate(pitches)]
    return _score_from_notes(notes)


# ──────────────────────────────────────────────
# 音名与按键名
# ──────────────────────────────────────────────

class NoteNameTest(unittest.TestCase):
    def test_note_name_uses_middle_c_equals_60(self):
        self.assertEqual(note_name(60), "C4")
        self.assertEqual(note_name(45), "A2")
        self.assertEqual(note_name(64), "E4")
        self.assertEqual(note_name(61), "C#4")
        self.assertEqual(note_name(0), "C-1")
        self.assertEqual(note_name(127), "G9")

    def test_note_name_out_of_range(self):
        self.assertEqual(note_name(200), "?200")

    def test_parse_note_name_accepts_common_spellings(self):
        self.assertEqual(parse_note_name("C4"), 60)
        self.assertEqual(parse_note_name("c4"), 60)
        self.assertEqual(parse_note_name("C#4"), 61)
        self.assertEqual(parse_note_name("C♯4"), 61)
        self.assertEqual(parse_note_name("Db4"), 61)
        self.assertEqual(parse_note_name("D♭4"), 61)
        self.assertEqual(parse_note_name(" c4 "), 60)
        self.assertEqual(parse_note_name("60"), 60)

    def test_parse_note_name_rejects_garbage(self):
        for text in ("", "H4", "C", "C99", "999", "xy", "-5"):
            self.assertIsNone(parse_note_name(text), text)

    def test_note_name_round_trip(self):
        for pitch in range(0, 128):
            self.assertEqual(parse_note_name(note_name(pitch)), pitch)


class KeyLabelTest(unittest.TestCase):
    def test_letters_digits_and_function_keys(self):
        self.assertEqual(key_to_vk("T"), 0x54)
        self.assertEqual(key_to_vk("t"), 0x54)
        self.assertEqual(key_to_vk("5"), 0x35)
        self.assertEqual(key_to_vk("F6"), 0x75)
        self.assertEqual(key_to_vk("space"), 0x20)
        self.assertEqual(key_to_vk("空格"), 0x20)

    def test_single_letter_f_is_not_function_key(self):
        # 手碟键位里 F 就是字母 F，不能被当成 F 功能键的前缀
        self.assertEqual(normalize_key_label("F"), "F")
        self.assertEqual(key_to_vk("F"), 0x46)

    def test_punctuation_and_rejections(self):
        self.assertEqual(normalize_key_label(";"), ";")
        self.assertIsNone(normalize_key_label(""))
        self.assertIsNone(normalize_key_label("Ctrl"))
        self.assertIsNone(key_to_vk("不存在的键"))

    def test_default_handpan_bindings_all_resolvable(self):
        layout = KeyLayout.default()
        self.assertEqual(len(layout), 9)
        for binding in layout.bindings:
            self.assertIsNotNone(key_to_vk(binding.key), binding)


# ──────────────────────────────────────────────
# 键位表
# ──────────────────────────────────────────────

class KeyLayoutTest(unittest.TestCase):
    def test_default_layout_matches_roco_handpan(self):
        """出厂键位必须与《洛克王国：世界》手碟九键一致。"""
        layout = KeyLayout.default()
        self.assertEqual(
            [(b.pitch, b.key) for b in layout.bindings],
            [(45, "B"), (52, "F"), (53, "G"), (55, "H"), (57, "J"),
             (59, "K"), (60, "T"), (62, "Y"), (64, "U")],
        )
        self.assertEqual(layout.range_text(), "A2 ~ E4（9 键）")
        self.assertEqual(layout.conflicts(), [])

    def test_default_layout_note_degrees(self):
        """九键对应的手碟数字谱：低音 6、3~7、高音 1~3。"""
        layout = KeyLayout.default()
        self.assertEqual(
            {b.key: b.note for b in layout.bindings},
            {"B": "低音 6", "F": "3", "G": "4", "H": "5", "J": "6",
             "K": "7", "T": "高音 1", "Y": "高音 2", "U": "高音 3"},
        )

    def test_add_replaces_same_pitch_and_keeps_sorted(self):
        layout = KeyLayout.default()
        layout.add(KeyBinding(60, "Q"))
        self.assertEqual(layout.key_for(60), "Q")
        self.assertEqual(layout.pitches, sorted(layout.pitches))
        self.assertEqual(len(layout), 9)

    def test_conflict_detection(self):
        layout = KeyLayout([KeyBinding(60, "T"), KeyBinding(62, "T")])
        self.assertEqual(layout.conflicts(), ["T"])
        self.assertEqual(layout.key_for(60), "T")
        self.assertEqual(layout.key_for(62), "T")

    def test_add_rejects_bad_input(self):
        layout = KeyLayout.default()
        with self.assertRaises(ValueError):
            layout.add(KeyBinding(200, "T"))
        with self.assertRaises(ValueError):
            layout.add(KeyBinding(60, "Ctrl"))

    def test_remove(self):
        layout = KeyLayout.default()
        self.assertTrue(layout.remove(60))
        self.assertFalse(layout.remove(60))
        self.assertIsNone(layout.key_for(60))
        self.assertEqual(len(layout), 8)

    def test_to_list_from_list_round_trip(self):
        layout = KeyLayout.default()
        restored = KeyLayout.from_list(layout.to_list())
        self.assertEqual(
            [(b.pitch, b.key, b.note) for b in restored.bindings],
            [(b.pitch, b.key, b.note) for b in layout.bindings],
        )

    def test_from_list_skips_invalid_entries(self):
        layout = KeyLayout.from_list([
            {"pitch": 60, "key": "T"},
            {"pitch": "abc", "key": "Y"},
            {"pitch": 62, "key": "不存在"},
            {"pitch": 999, "key": "U"},
            "not-a-dict",
            {"pitch": 64, "key": "u"},
        ])
        self.assertEqual([(b.pitch, b.key) for b in layout.bindings],
                         [(60, "T"), (64, "U")])

    def test_nearest_pitch(self):
        layout = KeyLayout.default()
        self.assertEqual(layout.nearest_pitch(61), 60)   # C#4 距 C4/D4 等远，取低者
        self.assertEqual(layout.nearest_pitch(63), 62)
        self.assertEqual(layout.nearest_pitch(100), 64)
        self.assertIsNone(KeyLayout().nearest_pitch(60))

    def test_empty_layout(self):
        layout = KeyLayout()
        self.assertEqual(len(layout), 0)
        self.assertEqual(layout.range_text(), "未绑定任何按键")
        self.assertIsNone(layout.key_for(60))


# ──────────────────────────────────────────────
# MIDI 解析
# ──────────────────────────────────────────────

class ParseMidiTest(unittest.TestCase):
    def test_format0_single_track_timing(self):
        """120 BPM / ppq=480 → 一拍 0.5 秒。"""
        tmp, score = _score_from_notes([(0.0, 0.5, 60), (0.5, 0.25, 62)],
                                       midi_format=0)
        self.addCleanup(tmp.cleanup)

        self.assertEqual(score.midi_format, 0)
        self.assertEqual(score.ppq, PPQ)
        self.assertEqual(len(score.notes), 2)
        self.assertAlmostEqual(score.notes[0].start, 0.0, places=6)
        self.assertAlmostEqual(score.notes[0].duration, 0.5, places=6)
        self.assertEqual(score.notes[0].pitch, 60)
        self.assertAlmostEqual(score.notes[1].start, 0.5, places=6)
        self.assertAlmostEqual(score.notes[1].duration, 0.25, places=6)
        self.assertAlmostEqual(score.bpm, 120.0, places=3)
        self.assertAlmostEqual(score.duration, 0.75, places=6)
        self.assertEqual(score.pitch_range, (60, 62))

    def test_note_on_with_zero_velocity_is_note_off(self):
        track = (
            _tempo(0, 120)
            + _note_on(0, 0, 60, 90)
            + _note_on(480, 0, 60, 0)     # 力度 0 的 note on 等价于 note off
            + _end_of_track()
        )
        tmp, path = _write_tmp(_build_midi([track]))
        self.addCleanup(tmp.cleanup)
        score = parse_midi(path)
        self.assertEqual(len(score.notes), 1)
        self.assertAlmostEqual(score.notes[0].duration, 0.5, places=6)
        self.assertEqual(score.notes[0].velocity, 90)

    def test_tempo_change_mid_track(self):
        """前 480 tick 是 120 BPM，之后 60 BPM：第二段的一拍是 1 秒。"""
        track = (
            _tempo(0, 120)
            + _note_on(0, 0, 60)
            + _note_off(480, 0, 60)
            + _tempo(0, 60)
            + _note_on(0, 0, 62)          # tick 480 → 0.5s
            + _note_off(480, 0, 62)       # tick 960 → 1.5s
            + _end_of_track()
        )
        tmp, path = _write_tmp(_build_midi([track]))
        self.addCleanup(tmp.cleanup)
        score = parse_midi(path)

        self.assertEqual(score.tempo_changes, 2)
        second = next(n for n in score.notes if n.pitch == 62)
        self.assertAlmostEqual(second.start, 0.5, places=6)
        self.assertAlmostEqual(second.duration, 1.0, places=6)

    def test_tempo_in_separate_track_applies_to_all(self):
        """format 1 里速度通常单独放在第 0 轨，音符轨必须沿用同一张速度表。"""
        tempo_track = _tempo(0, 60) + _end_of_track()      # 一拍 1 秒
        melody = _note_on(0, 0, 60) + _note_off(480, 0, 60) + _end_of_track()
        tmp, path = _write_tmp(_build_midi([tempo_track, melody]))
        self.addCleanup(tmp.cleanup)
        score = parse_midi(path)

        self.assertEqual(len(score.notes), 1)
        self.assertAlmostEqual(score.notes[0].duration, 1.0, places=6)

    def test_multi_track_with_names_and_channels(self):
        tempo_track = _tempo(0, 100) + _track_name(0, "速度轨") + _end_of_track()
        melody = (_track_name(0, "主旋律") + _note_on(0, 3, 60)
                  + _note_off(100, 3, 60) + _end_of_track())
        bass = (_track_name(0, "伴奏") + _note_on(0, 5, 45)
                + _note_off(100, 5, 45) + _end_of_track())
        tmp, path = _write_tmp(_build_midi([tempo_track, melody, bass]))
        self.addCleanup(tmp.cleanup)
        score = parse_midi(path)

        self.assertEqual(score.midi_format, 1)
        self.assertEqual(len(score.tracks), 3)
        self.assertEqual([t.name for t in score.tracks], ["速度轨", "主旋律", "伴奏"])
        self.assertEqual([t.note_count for t in score.tracks], [0, 1, 1])
        # 两轨的音符起点相同，排序按音高，所以按 pitch 取出来核对归属
        melody_note = next(n for n in score.notes if n.pitch == 60)
        bass_note = next(n for n in score.notes if n.pitch == 45)
        self.assertEqual(melody_note.track_name, "主旋律")
        self.assertEqual(melody_note.channel, 3)
        self.assertEqual(melody_note.track, 1)
        self.assertEqual(bass_note.track_name, "伴奏")
        self.assertEqual(bass_note.channel, 5)
        self.assertEqual(bass_note.track, 2)

    def test_gbk_track_name_is_decoded(self):
        track = (_tempo(0, 120) + _track_name(0, "主旋律", encoding="gbk")
                 + _note_on(0, 0, 60) + _note_off(480, 0, 60) + _end_of_track())
        tmp, path = _write_tmp(_build_midi([track]))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(parse_midi(path).tracks[0].name, "主旋律")

    def test_binary_garbage_track_name_falls_back_to_index(self):
        """解不出可读文本时退回「轨道 N」，而不是显示一串乱码。"""
        body = (
            _tempo(0, 120)
            + _varint(0) + b"\xff\x03\x06" + bytes.fromhex("2a7d0d4b9ff2")
            + _note_on(0, 0, 60) + _note_off(480, 0, 60)
            + _end_of_track()
        )
        tmp, path = _write_tmp(_build_midi([body]))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(parse_midi(path).tracks[0].name, "轨道 1")

    def test_running_status(self):
        """连续同状态音符会省略状态字节，解析器必须沿用 running status。"""
        body = (
            _tempo(0, 120)
            + _note_on(0, 0, 60)
            + _varint(0) + bytes([60, 0])       # running status 的 note on(vel=0)
            + _varint(0) + bytes([0x90, 62, 100])
            + _varint(480) + bytes([62, 0])
            + _end_of_track()
        )
        tmp, path = _write_tmp(_build_midi([body]))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(sorted(n.pitch for n in parse_midi(path).notes), [60, 62])

    def test_program_change_single_data_byte(self):
        body = (
            _tempo(0, 120)
            + _varint(0) + bytes([0xC0, 0x19])          # program change
            + _varint(0) + bytes([0xD0, 0x40])          # channel aftertouch
            + _note_on(0, 0, 60) + _note_off(480, 0, 60)
            + _end_of_track()
        )
        tmp, path = _write_tmp(_build_midi([body]))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(len(parse_midi(path).notes), 1)

    def test_dangling_note_gets_closed_at_track_end(self):
        """缺少 note off 的音符补到轨道末尾（end-of-track 所在的 tick）。"""
        track = (_tempo(0, 120) + _note_on(0, 0, 60)
                 + _varint(960) + b"\xff\x2f\x00")
        tmp, path = _write_tmp(_build_midi([track]))
        self.addCleanup(tmp.cleanup)
        score = parse_midi(path)
        self.assertEqual(len(score.notes), 1)
        self.assertAlmostEqual(score.notes[0].duration, 1.0, places=6)

    def test_all_notes_off_releases_pending(self):
        track = (
            _tempo(0, 120)
            + _note_on(0, 0, 60)
            + _note_on(0, 0, 64)
            + _varint(480) + bytes([0xB0, 123, 0])   # All Notes Off
            + _end_of_track()
        )
        tmp, path = _write_tmp(_build_midi([track]))
        self.addCleanup(tmp.cleanup)
        score = parse_midi(path)
        self.assertEqual(len(score.notes), 2)
        for note in score.notes:
            self.assertAlmostEqual(note.duration, 0.5, places=6)

    def test_overlapping_same_pitch_uses_fifo(self):
        """同音高叠置时按先来先配对，避免时长算错。"""
        track = (
            _tempo(0, 120)
            + _note_on(0, 0, 60)          # tick 0
            + _note_on(240, 0, 60)        # tick 240
            + _note_off(240, 0, 60)       # tick 480 → 闭合第一个（0.5s）
            + _note_off(0, 0, 60)         # tick 480 → 闭合第二个（0.25s）
            + _end_of_track()
        )
        tmp, path = _write_tmp(_build_midi([track]))
        self.addCleanup(tmp.cleanup)
        score = parse_midi(path)
        self.assertEqual(len(score.notes), 2)
        self.assertEqual(sorted(round(n.duration, 6) for n in score.notes), [0.25, 0.5])

    def test_sysex_is_skipped(self):
        track = (
            _tempo(0, 120)
            + _varint(0) + b"\xf0" + _varint(4) + b"\x7e\x7f\x09\x01"
            + _note_on(0, 0, 60) + _note_off(480, 0, 60)
            + _varint(0) + b"\xf7" + _varint(2) + b"\x00\x01"
            + _end_of_track()
        )
        tmp, path = _write_tmp(_build_midi([track]))
        self.addCleanup(tmp.cleanup)
        self.assertEqual(len(parse_midi(path).notes), 1)

    def test_declared_track_count_mismatch_is_tolerated(self):
        track = _tempo(0, 120) + _note_on(0, 0, 60) + _note_off(480, 0, 60) + _end_of_track()
        data = (b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big")
                + (9).to_bytes(2, "big") + PPQ.to_bytes(2, "big")
                + b"MTrk" + len(track).to_bytes(4, "big") + track)
        tmp, path = _write_tmp(data)
        self.addCleanup(tmp.cleanup)
        self.assertEqual(len(parse_midi(path).notes), 1)

    def test_rejects_non_midi_and_missing_files(self):
        tmp, bogus = _write_tmp(b"not a midi file at all", "bogus.mid")
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(MidiParseError):
            parse_midi(bogus)
        with self.assertRaises(FileNotFoundError):
            parse_midi(Path(tmp.name) / "missing.mid")

    def test_rejects_file_without_tracks(self):
        data = (b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big")
                + (0).to_bytes(2, "big") + PPQ.to_bytes(2, "big"))
        tmp, path = _write_tmp(data)
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(MidiParseError):
            parse_midi(path)

    def test_rejects_zero_division(self):
        track = _tempo(0, 120) + _end_of_track()
        data = (b"MThd" + (6).to_bytes(4, "big") + (1).to_bytes(2, "big")
                + (1).to_bytes(2, "big") + (0).to_bytes(2, "big")
                + b"MTrk" + len(track).to_bytes(4, "big") + track)
        tmp, path = _write_tmp(data)
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(MidiParseError):
            parse_midi(path)

    def test_empty_score_has_no_notes(self):
        tmp, path = _write_tmp(_build_midi([_tempo(0, 120) + _end_of_track()]))
        self.addCleanup(tmp.cleanup)
        score = parse_midi(path)
        self.assertEqual(score.notes, [])
        self.assertEqual(score.pitch_range, (None, None))
        self.assertFalse(check_score(score, KeyLayout.default()).ok)


class SampleScoreTest(unittest.TestCase):
    """用仓库自带的示例曲谱做一次端到端校验。"""

    @unittest.skipUnless(SAMPLE_MIDI.exists(), "示例曲谱不存在")
    def test_callofslience_is_fully_playable(self):
        score = parse_midi(SAMPLE_MIDI)
        self.assertEqual(len(score.notes), 276)
        self.assertEqual(score.ppq, PPQ)
        self.assertAlmostEqual(score.bpm, 120.0, places=3)
        # RocoMusic 曲谱页标注：120 BPM、时长 1 分 51 秒
        self.assertEqual(score.duration_text(), "1:51")
        self.assertEqual(score.pitch_range, (45, 64))
        # 示例曲谱用的正好是手碟九键，一个音都不缺
        self.assertEqual(sorted({n.pitch for n in score.notes}),
                         [45, 52, 53, 55, 57, 59, 60, 62, 64])

        result = check_score(score, KeyLayout.default())
        self.assertTrue(result.ok, result.describe_missing())
        self.assertEqual(result.playable_notes, 276)
        self.assertEqual(result.missing, [])
        self.assertEqual(best_shift(score, KeyLayout.default()), 0)

        performance = build_performance(score, KeyLayout.default())
        self.assertEqual(performance.note_count, 276)
        self.assertEqual(performance.skipped, 0)
        self.assertGreater(len(performance.events), 0)


# ──────────────────────────────────────────────
# 可演奏性检查
# ──────────────────────────────────────────────

class CheckScoreTest(unittest.TestCase):
    def test_all_bound_notes_pass(self):
        tmp, score = _score_with_pitches([60, 62, 64])
        self.addCleanup(tmp.cleanup)
        result = check_score(score, KeyLayout.default())
        self.assertTrue(result.ok)
        self.assertEqual(result.total_notes, 3)
        self.assertEqual(result.playable_notes, 3)
        self.assertEqual(result.missing_notes, 0)
        self.assertEqual(result.describe_missing(), "")

    def test_unbound_note_is_reported_with_details(self):
        # C5(72) 和 F#4(66) 都不在手碟九键里
        tmp, score = _score_with_pitches([60, 72, 60, 66])
        self.addCleanup(tmp.cleanup)
        result = check_score(score, KeyLayout.default())

        self.assertFalse(result.ok)
        self.assertEqual(result.total_notes, 4)
        self.assertEqual(result.playable_notes, 2)
        self.assertEqual(result.missing_notes, 2)

        by_pitch = {item.pitch: item for item in result.missing}
        self.assertEqual(set(by_pitch), {72, 66})
        self.assertEqual(by_pitch[72].name, "C5")
        self.assertEqual(by_pitch[72].count, 1)
        self.assertAlmostEqual(by_pitch[72].first_time, 0.5, places=6)
        self.assertEqual(by_pitch[72].nearest_name, "E4")
        self.assertEqual(by_pitch[72].nearest_semitones, 8)
        self.assertEqual(by_pitch[66].count, 1)

        text = result.describe_missing()
        self.assertIn("C5", text)
        self.assertIn("F#4", text)
        self.assertIn("没有绑定按键", text)
        self.assertIn("50.0%", text)

    def test_missing_sorted_by_count_then_pitch(self):
        tmp, score = _score_with_pitches([72, 72, 72, 66, 66, 61])
        self.addCleanup(tmp.cleanup)
        result = check_score(score, KeyLayout.default())
        self.assertEqual([item.pitch for item in result.missing], [72, 66, 61])

    def test_describe_missing_can_be_truncated(self):
        pitches = [70, 71, 72, 73, 74]
        tmp, score = _score_with_pitches(pitches)
        self.addCleanup(tmp.cleanup)
        result = check_score(score, KeyLayout.default())
        self.assertEqual(result.missing_pitches, 5)
        truncated = result.describe_missing(limit=2)
        self.assertIn("另有 3 个音未列出", truncated)
        self.assertEqual(truncated.count("·"), 2)

    def test_shift_makes_unplayable_score_playable(self):
        # 整首曲子比手碟音域高 12 个半音，降一个八度即可完整演奏
        tmp, score = _score_with_pitches([72, 74, 76])
        self.addCleanup(tmp.cleanup)
        self.assertFalse(check_score(score, KeyLayout.default()).ok)
        self.assertTrue(check_score(score, KeyLayout.default(), shift=-12).ok)
        self.assertEqual(best_shift(score, KeyLayout.default()), -12)

    def test_shift_is_clamped_to_midi_range(self):
        tmp, score = _score_with_pitches([126, 127])
        self.addCleanup(tmp.cleanup)
        result = check_score(score, KeyLayout.default(), shift=12)
        self.assertFalse(result.ok)
        # 移调后被夹到 127，报告里的音高也是夹过的值
        self.assertEqual({item.pitch for item in result.missing}, {127})

    def test_no_shift_can_save_a_chromatic_score(self):
        tmp, score = _score_with_pitches(list(range(60, 72)))
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(best_shift(score, KeyLayout.default()))
        candidates = find_shifts(score, KeyLayout.default())
        self.assertEqual(len(candidates), 49)
        self.assertEqual(candidates[0][1], max(c[1] for c in candidates))

    def test_find_shifts_prefers_smallest_absolute_shift(self):
        tmp, score = _score_with_pitches([57, 59, 60])   # A3 B3 C4，本身是九键子集
        self.addCleanup(tmp.cleanup)
        candidates = find_shifts(score, KeyLayout.default())
        self.assertEqual(candidates[0][0], 0)
        self.assertEqual(candidates[0][1], 3)

    def test_find_shifts_needs_a_layout(self):
        tmp, score = _score_with_pitches([60])
        self.addCleanup(tmp.cleanup)
        self.assertEqual(find_shifts(score, KeyLayout()), [])

    def test_density_and_overlap_diagnostics(self):
        notes = [(0.0, 1.0, 60), (0.2, 0.3, 60), (0.4, 0.2, 62),
                 (0.6, 0.2, 64), (0.8, 0.2, 57)]
        tmp, score = _score_from_notes(notes)
        self.addCleanup(tmp.cleanup)

        result = check_score(score, KeyLayout.default())
        self.assertTrue(result.ok)
        self.assertEqual(result.peak_notes_per_second, 5)
        # 只有 0.2s 那个 C4 落在前一个 C4 的时值里
        self.assertEqual(result.same_key_overlaps, 1)

    def test_summary_line(self):
        tmp, score = _score_with_pitches([60, 62])
        self.addCleanup(tmp.cleanup)
        result = check_score(score, KeyLayout.default())
        self.assertIn("2 个音符", result.summary())
        self.assertIn("C4~D4", result.summary())
        self.assertIn("120 BPM", result.summary())
        self.assertNotIn("移调", result.summary())
        self.assertIn("移调 -12 半音",
                      check_score(score, KeyLayout.default(), shift=-12).summary())


# ──────────────────────────────────────────────
# 编译成按键事件
# ──────────────────────────────────────────────

class BuildPerformanceTest(unittest.TestCase):
    def test_every_note_produces_one_key_press(self):
        tmp, score = _score_with_pitches([60, 62, 64])
        self.addCleanup(tmp.cleanup)
        performance = build_performance(score, KeyLayout.default())

        downs = [e for e in performance.events if e.down]
        ups = [e for e in performance.events if not e.down]
        self.assertEqual(len(downs), 3)
        self.assertEqual(len(ups), 3)
        self.assertEqual(performance.note_count, 3)
        self.assertEqual(performance.skipped, 0)
        self.assertEqual(performance.key_count, 3)
        self.assertEqual([e.key for e in downs], ["T", "Y", "U"])
        self.assertEqual([e.vk for e in downs], [0x54, 0x59, 0x55])

    def test_hold_ratio_shortens_release(self):
        tmp, score = _score_with_pitches([60], duration=0.4)
        self.addCleanup(tmp.cleanup)
        performance = build_performance(
            score, KeyLayout.default(),
            options=PerformanceOptions(hold_ratio=0.5, min_hold_ms=10,
                                       retrigger_gap_ms=12),
        )
        down, up = performance.events
        self.assertTrue(down.down)
        self.assertAlmostEqual(down.time, 0.0, places=6)
        self.assertAlmostEqual(up.time, 0.2, places=6)   # 0.4 * 0.5

    def test_min_hold_floor(self):
        tmp, score = _score_with_pitches([60], duration=0.001)
        self.addCleanup(tmp.cleanup)
        performance = build_performance(
            score, KeyLayout.default(),
            options=PerformanceOptions(min_hold_ms=45))
        down, up = performance.events
        self.assertAlmostEqual(up.time - down.time, 0.045, places=6)

    def test_same_key_overlap_is_retriggered(self):
        """同键重叠音符必须提前抬手，否则第二个音不会发声。"""
        tmp, score = _score_from_notes([(0.0, 1.0, 60), (0.5, 0.5, 60)])
        self.addCleanup(tmp.cleanup)

        options = PerformanceOptions(hold_ratio=0.9, min_hold_ms=45,
                                     retrigger_gap_ms=12)
        performance = build_performance(score, KeyLayout.default(), options=options)
        self.assertEqual(len(performance.events), 4)
        first_down, first_up, second_down, second_up = performance.events
        self.assertTrue(first_down.down)
        self.assertFalse(first_up.down)
        self.assertTrue(second_down.down)
        self.assertFalse(second_up.down)
        # 第一个音在第二个音按下前 12ms 抬手
        self.assertAlmostEqual(first_up.time, 0.488, places=6)
        self.assertAlmostEqual(second_down.time, 0.5, places=6)
        self.assertLess(first_up.time, second_down.time)

    def test_well_formed_event_stream_under_heavy_overlap(self):
        """全局事件序列里每个按键都必须严格 down/up 交替，且结束时全部抬手。"""
        pitches = [60, 60, 62, 60, 64, 62, 45, 57, 59, 52, 53, 55, 60, 60]
        tmp, score = _score_with_pitches(pitches, duration=0.35, spacing=0.2)
        self.addCleanup(tmp.cleanup)
        performance = build_performance(score, KeyLayout.default())

        open_keys: set[str] = set()
        for event in performance.events:
            if event.down:
                self.assertNotIn(event.key, open_keys,
                                 f"{event.key} 在未抬手时又被按下")
                open_keys.add(event.key)
            else:
                self.assertIn(event.key, open_keys, f"{event.key} 抬手前没有按下")
                open_keys.discard(event.key)
        self.assertEqual(open_keys, set(), "演奏结束时仍有按键被按住")
        self.assertEqual(performance.note_count, len(pitches))
        self.assertEqual(len([e for e in performance.events if e.down]), len(pitches))

        times = [e.time for e in performance.events]
        self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(performance.duration, times[-1])

    def test_unbound_notes_are_skipped_and_counted(self):
        tmp, score = _score_with_pitches([60, 72, 62])
        self.addCleanup(tmp.cleanup)
        performance = build_performance(score, KeyLayout.default())
        self.assertEqual(performance.skipped, 1)
        self.assertEqual(performance.note_count, 2)
        self.assertEqual({e.pitch for e in performance.events}, {60, 62})

    def test_shift_applies_to_output_pitches(self):
        tmp, score = _score_with_pitches([72, 74])
        self.addCleanup(tmp.cleanup)
        performance = build_performance(score, KeyLayout.default(), shift=-12)
        self.assertEqual(sorted({e.pitch for e in performance.events}), [60, 62])
        self.assertEqual(performance.skipped, 0)

    def test_empty_score_builds_empty_performance(self):
        tmp, path = _write_tmp(_build_midi([_tempo(0, 120) + _end_of_track()]))
        self.addCleanup(tmp.cleanup)
        performance = build_performance(parse_midi(path), KeyLayout.default())
        self.assertEqual(performance.events, [])
        self.assertEqual(performance.duration, 0.0)

    def test_max_polyphony_drops_lowest_voice(self):
        notes = [(0.0, 1.0, 45), (0.0, 1.0, 57), (0.0, 1.0, 64), (0.5, 0.5, 60)]
        tmp, score = _score_from_notes(notes)
        self.addCleanup(tmp.cleanup)

        full = build_performance(score, KeyLayout.default())
        self.assertEqual(full.note_count, 4)
        limited = build_performance(
            score, KeyLayout.default(),
            options=PerformanceOptions(max_polyphony=3))
        self.assertEqual(limited.note_count, 3)
        # 挤掉的是音高最低的 A2，高声部旋律保留
        self.assertNotIn(45, {e.pitch for e in limited.events})
        self.assertIn(60, {e.pitch for e in limited.events})
        self.assertIn(64, {e.pitch for e in limited.events})


# ──────────────────────────────────────────────
# 曲谱库
# ──────────────────────────────────────────────

class MusicLibraryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.library = MusicLibrary(self.data_dir)

    def _make_source(self, name: str) -> Path:
        source = self.data_dir / name
        track = _track_from_notes([(0.0, 0.5, 60)])
        source.write_bytes(_build_midi([track]))
        return source

    def test_list_is_empty_before_dir_exists(self):
        self.assertEqual(self.library.list_scores(), [])

    def test_import_list_resolve_delete(self):
        imported = self.library.import_file(self._make_source("outside.mid"))
        self.assertIsNotNone(imported)
        self.assertEqual(imported.parent, self.library.music_dir)

        listed = self.library.list_scores()
        self.assertEqual([item["name"] for item in listed], ["outside"])
        self.assertEqual(listed[0]["filename"], "outside.mid")
        self.assertGreater(listed[0]["size"], 0)

        self.assertEqual(self.library.resolve("outside"), imported)
        self.assertEqual(self.library.resolve("outside.mid"), imported)
        self.assertIsNone(self.library.resolve("nope"))
        self.assertIsNone(self.library.resolve(""))

        self.assertTrue(self.library.delete("outside"))
        self.assertFalse(self.library.delete("outside"))
        self.assertEqual(self.library.list_scores(), [])

    def test_import_renames_on_conflict(self):
        source = self._make_source("song.mid")
        self.library.import_file(source)
        second = self.library.import_file(source)
        self.assertEqual([item["name"] for item in self.library.list_scores()],
                         ["song", "song_2"])
        self.assertEqual(second.name, "song_2.mid")

    def test_import_rejects_non_midi(self):
        text_file = self.data_dir / "notes.txt"
        text_file.write_text("not midi", encoding="utf-8")
        self.assertIsNone(self.library.import_file(text_file))
        self.assertIsNone(self.library.import_file(self.data_dir / "missing.mid"))

    def test_list_ignores_non_midi_and_subdirectories(self):
        self.library.ensure_dir()
        self._make_source("a.mid")
        self.library.import_file(self.data_dir / "a.mid")
        (self.library.music_dir / "readme.txt").write_text("x", encoding="utf-8")
        (self.library.music_dir / "nested").mkdir()
        self.assertEqual([item["name"] for item in self.library.list_scores()], ["a"])

    def test_midi_extension_is_listed(self):
        self.library.import_file(self._make_source("b.midi"))
        self.assertEqual([item["filename"] for item in self.library.list_scores()],
                         ["b.midi"])

    def test_music_dir_lives_under_data(self):
        self.assertEqual(self.library.music_dir, self.data_dir / "music")


if __name__ == "__main__":
    unittest.main(verbosity=2)
