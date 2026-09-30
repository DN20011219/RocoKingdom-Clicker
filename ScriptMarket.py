"""
脚本市场模块 - V1 静态市场

包含四个部分：
1. MarketIndexClient   —— 拉取市场索引 / 下载脚本文件（urllib，仅标准库）
2. SchemaValidator     —— 脚本包 v2 安全校验（导入/上传两端复用）
3. ScriptAdapter       —— 本机环境探测、分辨率归一化换算、环境兼容性比对
4. MarketImporter / MarketExporter —— 下载导入落盘 / 本地脚本打包导出

脚本包格式 v2（向后兼容 v1）：
{
  "format_version": 2,
  "market": {"id", "title", "author", "version", "game", "tags", "description"},
  "environment": {"resolution": [w, h], "dpi_scale", "pointer_speed",
                  "mouse_acceleration", "game_mode"},
  "name": "...",
  "actions": [...]   # click/move 动作可携带 x_norm/y_norm 归一化坐标
}
"""

from __future__ import annotations

import copy
import ctypes
import json
import logging
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import winreg
except ImportError:  # 非 Windows 环境（理论上不会发生）
    winreg = None

logger = logging.getLogger("script_market")

# ── 格式与安全限制 ──────────────────────────────
MARKET_FORMAT_VERSION = 2
MAX_SCRIPT_BYTES = 512 * 1024      # 单个脚本包大小上限 512KB
MAX_NESTING_DEPTH = 8              # 动作嵌套深度上限
MAX_TOTAL_ACTIONS = 10000          # 动作总数上限（含嵌套）

ACTION_TYPES = {"click", "move", "key", "combo", "wait", "loop", "timed"}

# 每种动作类型允许的字段（白名单）
ALLOWED_ACTION_FIELDS = {
    "click": {"type", "x", "y", "hold_ms", "x_jitter_px", "y_jitter_px",
              "hold_jitter_ms", "x_norm", "y_norm"},
    "move": {"type", "x", "y", "duration_ms", "x_jitter_px", "y_jitter_px",
             "duration_jitter_ms", "x_norm", "y_norm"},
    "key": {"type", "vk_code", "hold_ms", "hold_jitter_ms"},
    "combo": {"type", "vk_codes", "hold_ms", "hold_jitter_ms"},
    "wait": {"type", "duration_ms", "duration_jitter_ms"},
    "loop": {"type", "count", "forever", "until_exit", "actions",
             "pause_ms", "pause_jitter_ms"},
    "timed": {"type", "execute_ms", "sleep_ms", "repeat", "forever", "actions"},
}

TOP_LEVEL_FIELDS = {"format_version", "market", "environment",
                    "runtime_normalize", "meta", "name", "actions"}

MARKET_FIELDS = {"id", "title", "author", "version", "game", "tags", "description"}
ENVIRONMENT_FIELDS = {"resolution", "dpi_scale", "pointer_speed",
                      "mouse_acceleration", "game_mode"}

# 危险组合键黑名单（VK 集合，脚本中不允许出现）
DANGEROUS_COMBOS = [
    {0x11, 0x12, 0x2E},  # Ctrl+Alt+Del
    {0x5B, 0x4C},        # Win+L（锁屏）
    {0x5B, 0x44},        # Win+D（显示桌面）
    {0x5B, 0x52},        # Win+R（运行）
    {0x5B, 0x09},        # Win+Tab
]


class MarketValidationError(Exception):
    """脚本包校验失败，reason 为可直接展示给用户的原因。"""


class MarketError(Exception):
    """市场网络/IO 类错误。"""


def sanitize_market_id(market_id: str) -> str:
    """把市场 id 规范化为安全的文件名片段。"""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(market_id))
    return safe.strip("._") or "script"


def compare_versions(v1: str, v2: str) -> int:
    """简单版本号比较：-1 表示 v1<v2，0 相等，1 表示 v1>v2。"""
    def parse(v: str) -> List[int]:
        parts = re.findall(r"\d+", str(v or "0"))
        return [int(p) for p in parts] or [0]
    a, b = parse(v1), parse(v2)
    # 零补齐：2.0 与 2.0.0 视为相等
    length = max(len(a), len(b))
    a += [0] * (length - len(a))
    b += [0] * (length - len(b))
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


# ──────────────────────────────────────────────
# 1. 市场索引客户端
# ──────────────────────────────────────────────
class MarketIndexClient:
    """静态市场客户端：从 index_url 拉取索引，从 file_url 下载脚本。"""

    USER_AGENT = "RocoKingdomClicker/1.0 (+script-market)"

    def __init__(self, index_url: str, timeout: int = 8):
        self.index_url = index_url
        self.timeout = timeout

    @staticmethod
    def _check_scheme(url: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise MarketValidationError(f"仅支持 http/https 地址: {url}")

    def _read(self, url: str, max_bytes: Optional[int]) -> bytes:
        self._check_scheme(url)
        req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read(max_bytes + 1 if max_bytes else -1)
        if max_bytes and len(raw) > max_bytes:
            raise MarketValidationError(
                f"内容超过大小上限 {max_bytes // 1024}KB，已拒绝")
        return raw

    def fetch_index(self) -> Optional[List[dict]]:
        """拉取市场索引，返回脚本条目列表；失败返回 None（降级不影响本地功能）。"""
        if not self.index_url:
            return None
        try:
            raw = self._read(self.index_url, MAX_SCRIPT_BYTES)
            data = json.loads(raw.decode("utf-8"))
        except MarketValidationError as e:
            logger.warning("市场索引无效: %s", e)
            return None
        except Exception as e:
            logger.warning("拉取市场索引失败: %s", e)
            return None
        if isinstance(data, dict):
            data = data.get("scripts", [])
        if not isinstance(data, list):
            logger.warning("市场索引格式错误：期望脚本数组")
            return None
        return [item for item in data if isinstance(item, dict) and item.get("file_url")]

    def download_script(self, file_url: str) -> bytes:
        """下载脚本包原文，强制大小上限。"""
        try:
            return self._read(file_url, MAX_SCRIPT_BYTES)
        except MarketValidationError:
            raise
        except Exception as e:
            raise MarketError(f"下载脚本失败: {e}") from e


# ──────────────────────────────────────────────
# 2. 脚本包安全校验
# ──────────────────────────────────────────────
class SchemaValidator:
    """脚本包 v2 校验器（导入与导出两端复用）。"""

    @staticmethod
    def validate_package(data: Any, raw_size: int = 0) -> None:
        """校验整个脚本包，失败抛 MarketValidationError。"""
        if raw_size > MAX_SCRIPT_BYTES:
            raise MarketValidationError(
                f"脚本包超过大小上限 {MAX_SCRIPT_BYTES // 1024}KB")
        if not isinstance(data, dict):
            raise MarketValidationError("脚本包必须是 JSON 对象")

        unknown = set(data.keys()) - TOP_LEVEL_FIELDS
        if unknown:
            raise MarketValidationError(
                f"包含未知顶层字段: {', '.join(sorted(unknown))}")

        actions = data.get("actions")
        if not isinstance(actions, list):
            raise MarketValidationError("缺少 actions 数组")

        market = data.get("market")
        if market is not None:
            if not isinstance(market, dict):
                raise MarketValidationError("market 字段必须是对象")
            unknown = set(market.keys()) - MARKET_FIELDS
            if unknown:
                raise MarketValidationError(
                    f"market 包含未知字段: {', '.join(sorted(unknown))}")
            if int(data.get("format_version", 1)) >= 2 and not market.get("id"):
                raise MarketValidationError("v2 脚本包必须提供 market.id")
            tags = market.get("tags")
            if tags is not None and not (
                    isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
                raise MarketValidationError("market.tags 必须是字符串数组")

        environment = data.get("environment")
        if environment is not None:
            if not isinstance(environment, dict):
                raise MarketValidationError("environment 字段必须是对象")
            unknown = set(environment.keys()) - ENVIRONMENT_FIELDS
            if unknown:
                raise MarketValidationError(
                    f"environment 包含未知字段: {', '.join(sorted(unknown))}")
            resolution = environment.get("resolution")
            if resolution is not None and not (
                    isinstance(resolution, list) and len(resolution) == 2
                    and all(isinstance(v, (int, float)) and v > 0 for v in resolution)):
                raise MarketValidationError(
                    "environment.resolution 必须是 [宽, 高] 正数数组")

        SchemaValidator.validate_actions(actions)

    @staticmethod
    def validate_actions(actions: List[Any], depth: int = 0,
                         counter: Optional[List[int]] = None) -> None:
        """递归校验动作数组：类型白名单、字段白名单、深度/数量上限、危险组合键。"""
        if counter is None:
            counter = [0]
        if depth > MAX_NESTING_DEPTH:
            raise MarketValidationError(
                f"动作嵌套深度超过上限 {MAX_NESTING_DEPTH}")

        if not isinstance(actions, list):
            raise MarketValidationError("actions 必须是数组")

        for item in actions:
            counter[0] += 1
            if counter[0] > MAX_TOTAL_ACTIONS:
                raise MarketValidationError(
                    f"动作总数超过上限 {MAX_TOTAL_ACTIONS}")
            if not isinstance(item, dict):
                raise MarketValidationError("动作必须是 JSON 对象")

            action_type = item.get("type")
            if action_type not in ACTION_TYPES:
                raise MarketValidationError(f"未知动作类型: {action_type}")

            unknown = set(item.keys()) - ALLOWED_ACTION_FIELDS[action_type]
            if unknown:
                raise MarketValidationError(
                    f"{action_type} 动作包含未知字段: {', '.join(sorted(unknown))}")

            if action_type in ("click", "move"):
                for key in ("x", "y"):
                    if not isinstance(item.get(key), (int, float)) or item[key] < 0:
                        raise MarketValidationError(
                            f"{action_type} 动作的 {key} 必须是非负数")
                for key in ("x_norm", "y_norm"):
                    if key in item and not (
                            isinstance(item[key], (int, float)) and 0.0 <= item[key] <= 1.0):
                        raise MarketValidationError(
                            f"{action_type} 动作的 {key} 必须在 0~1 之间")

            if action_type == "key":
                SchemaValidator._check_vk(item.get("vk_code"), "key")

            if action_type == "combo":
                vk_codes = item.get("vk_codes")
                if not isinstance(vk_codes, list) or not vk_codes:
                    raise MarketValidationError("combo 动作的 vk_codes 必须是非空数组")
                for vk in vk_codes:
                    SchemaValidator._check_vk(vk, "combo")
                code_set = set(int(vk) for vk in vk_codes)
                for dangerous in DANGEROUS_COMBOS:
                    if dangerous.issubset(code_set):
                        names = "+".join(f"0x{vk:02X}" for vk in sorted(dangerous))
                        raise MarketValidationError(f"包含危险组合键: {names}")

            if action_type in ("loop", "timed"):
                SchemaValidator.validate_actions(
                    item.get("actions", []), depth + 1, counter)

    @staticmethod
    def _check_vk(vk: Any, context: str) -> None:
        if not isinstance(vk, int) or not (0 <= vk <= 255):
            raise MarketValidationError(
                f"{context} 动作的虚拟键码必须是 0~255 的整数")


# ──────────────────────────────────────────────
# 3. 环境适配层
# ──────────────────────────────────────────────
class ScriptAdapter:
    """本机环境探测 + 分辨率归一化换算 + 环境兼容性比对。"""

    @staticmethod
    def detect_local_env() -> Dict[str, Any]:
        """探测本机环境：分辨率、DPI 缩放、指针速度（1~11 档）、鼠标加速状态。

        无法探测的项置为 None，由 compare_env 决定是否提示。
        """
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))

        dpi_scale = 100
        try:
            dpi = int(user32.GetDpiForSystem())
            dpi_scale = max(100, round(dpi / 96 * 100))
        except Exception:
            pass

        pointer_speed: Optional[int] = None
        mouse_acceleration: Optional[bool] = None
        if winreg is not None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse") as key:
                    try:
                        sensitivity = int(winreg.QueryValueEx(key, "MouseSensitivity")[0])
                        # 注册表 1~20 映射为系统设置的 1~11 档（默认第 6 档 = 注册表 10）
                        pointer_speed = min(11, max(1, (sensitivity + 2) // 2))
                    except Exception:
                        pass
                    try:
                        mouse_speed = str(winreg.QueryValueEx(key, "MouseSpeed")[0])
                        mouse_acceleration = mouse_speed != "0"
                    except Exception:
                        pass
            except OSError:
                pass

        return {
            "resolution": [width, height],
            "dpi_scale": dpi_scale,
            "pointer_speed": pointer_speed,
            "mouse_acceleration": mouse_acceleration,
        }

    @staticmethod
    def normalize_actions(actions: List[dict], ref_resolution: List[float]) -> None:
        """原地给 click/move 动作补充 x_norm/y_norm（按参考分辨率归一化）。"""
        ref_w = max(float(ref_resolution[0]) - 1.0, 1.0)
        ref_h = max(float(ref_resolution[1]) - 1.0, 1.0)
        for item in actions:
            if not isinstance(item, dict):
                continue
            action_type = item.get("type")
            if action_type in ("click", "move"):
                x = float(item.get("x", 0))
                y = float(item.get("y", 0))
                item["x_norm"] = round(min(max(x / ref_w, 0.0), 1.0), 6)
                item["y_norm"] = round(min(max(y / ref_h, 0.0), 1.0), 6)
            elif action_type in ("loop", "timed") and isinstance(item.get("actions"), list):
                ScriptAdapter.normalize_actions(item["actions"], ref_resolution)

    @staticmethod
    def denormalize(package: Dict[str, Any], local_env: Dict[str, Any]) -> Dict[str, Any]:
        """导入时换算模式：按本机分辨率把归一化坐标换算为像素坐标并落盘。

        没有归一化坐标的动作保持原像素坐标不变。
        """
        result = copy.deepcopy(package)
        width = max(int(local_env.get("resolution", [1920, 1080])[0]) - 1, 1)
        height = max(int(local_env.get("resolution", [1920, 1080])[1]) - 1, 1)

        def walk(actions: List[dict]) -> None:
            for item in actions:
                if not isinstance(item, dict):
                    continue
                action_type = item.get("type")
                if action_type in ("click", "move"):
                    if isinstance(item.get("x_norm"), (int, float)) and \
                            isinstance(item.get("y_norm"), (int, float)):
                        item["x"] = int(round(float(item["x_norm"]) * width))
                        item["y"] = int(round(float(item["y_norm"]) * height))
                elif action_type in ("loop", "timed") and isinstance(item.get("actions"), list):
                    walk(item["actions"])

        walk(result.get("actions", []))
        result["runtime_normalize"] = False
        return result

    @staticmethod
    def compare_env(pkg_env: Optional[Dict[str, Any]],
                    local_env: Dict[str, Any]) -> List[Tuple[str, str]]:
        """比对脚本声明环境与本机环境，返回 [(level, message)]。

        level: "info" | "warn"
        """
        pkg_env = pkg_env or {}
        messages: List[Tuple[str, str]] = []

        pkg_res = pkg_env.get("resolution")
        local_res = local_env.get("resolution")
        if isinstance(pkg_res, list) and len(pkg_res) == 2 and local_res:
            if (int(pkg_res[0]), int(pkg_res[1])) == (int(local_res[0]), int(local_res[1])):
                messages.append(("info", f"分辨率与作者环境一致（{local_res[0]}x{local_res[1]}）"))
            else:
                messages.append((
                    "warn",
                    f"分辨率不一致：脚本基于 {int(pkg_res[0])}x{int(pkg_res[1])}，"
                    f"本机 {int(local_res[0])}x{int(local_res[1])}，坐标将按比例自动换算"))
                try:
                    pkg_ratio = float(pkg_res[0]) / float(pkg_res[1])
                    local_ratio = float(local_res[0]) / float(local_res[1])
                    if abs(pkg_ratio - local_ratio) > 0.01:
                        messages.append((
                            "warn",
                            f"宽高比不同（{pkg_ratio:.2f} vs {local_ratio:.2f}），"
                            "换算后界面元素可能变形，建议先用相同分辨率测试"))
                except ZeroDivisionError:
                    pass

        pkg_speed = pkg_env.get("pointer_speed")
        local_speed = local_env.get("pointer_speed")
        if pkg_speed and local_speed and int(pkg_speed) != int(local_speed):
            messages.append((
                "info",
                f"鼠标指针速度不一致（作者 {pkg_speed}/11，本机 {local_speed}/11）："
                "本脚本使用绝对坐标不受影响，但若游戏内有视角灵敏度请注意"))

        pkg_accel = pkg_env.get("mouse_acceleration")
        local_accel = local_env.get("mouse_acceleration")
        if pkg_accel is False and local_accel is True:
            messages.append((
                "warn",
                "本机未关闭「提高指针精确度」（鼠标加速），"
                "本项目要求关闭以保证定位精度，建议在系统设置中关闭"))
        elif local_accel is False:
            messages.append(("info", "本机已关闭鼠标加速，符合要求"))

        return messages

    @staticmethod
    def scale_relative(dx: float, dy: float,
                       pkg_env: Optional[Dict[str, Any]],
                       local_env: Dict[str, Any]) -> Tuple[float, float]:
        """预留接口：相对位移按指针速度比例缩放。

        现有动作均为绝对坐标，V1 不调用；未来引入相对位移动作时使用。
        """
        pkg_speed = (pkg_env or {}).get("pointer_speed")
        local_speed = local_env.get("pointer_speed")
        if pkg_speed and local_speed and pkg_speed > 0:
            ratio = float(local_speed) / float(pkg_speed)
            return dx * ratio, dy * ratio
        return dx, dy


# ──────────────────────────────────────────────
# 4. 导入 / 导出
# ──────────────────────────────────────────────
class MarketConfig:
    """市场配置（data/clicker_configs/market.json）。"""

    DEFAULTS = {"index_url": "", "repo_url": ""}

    def __init__(self, configs_dir: Path):
        self.path = Path(configs_dir) / "market.json"

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                merged = dict(self.DEFAULTS)
                merged.update({k: v for k, v in data.items() if k in self.DEFAULTS})
                return merged
        except Exception:
            pass
        return dict(self.DEFAULTS)

    def save(self, config: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)


class MarketImporter:
    """把下载到的脚本包导入 data/action_scripts/。"""

    def __init__(self, scripts_dir: Path, configs_dir: Path):
        self.scripts_dir = Path(scripts_dir)
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        self.installed_path = Path(configs_dir) / "market_installed.json"

    # ── 已安装记录 ──
    def get_installed(self) -> Dict[str, dict]:
        try:
            with open(self.installed_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _record_installed(self, market_id: str, version: str, file_name: str) -> None:
        installed = self.get_installed()
        installed[market_id] = {
            "version": version,
            "file": file_name,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.installed_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.installed_path, "w", encoding="utf-8") as fh:
            json.dump(installed, fh, ensure_ascii=False, indent=2)

    def installed_status(self, entry: Dict[str, Any]) -> str:
        """对照索引条目返回 "not_installed" | "up_to_date" | "update_available"。"""
        market_id = entry.get("id") or ""
        record = self.get_installed().get(market_id)
        if not record:
            return "not_installed"
        if compare_versions(entry.get("version", ""), record.get("version", "")) > 0:
            return "update_available"
        return "up_to_date"

    # ── 导入主流程 ──
    def import_raw(self, raw: bytes, mode: str = "denormalize") -> Dict[str, Any]:
        """导入脚本包原文。

        mode:
          - "denormalize"：导入时按本机分辨率换算坐标落盘（默认）
          - "runtime"：保留归一化坐标，运行时动态换算
        返回 {"file": 路径, "stem": 文件名, "issues": 兼容性提示, "package": 落盘内容}
        """
        if len(raw) > MAX_SCRIPT_BYTES:
            raise MarketValidationError(
                f"脚本包超过大小上限 {MAX_SCRIPT_BYTES // 1024}KB")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise MarketValidationError(f"脚本包不是合法 JSON: {e}") from e

        SchemaValidator.validate_package(data, len(raw))

        local_env = ScriptAdapter.detect_local_env()
        issues = ScriptAdapter.compare_env(data.get("environment"), local_env)

        if mode == "runtime":
            package = copy.deepcopy(data)
            package["runtime_normalize"] = True
        else:
            package = ScriptAdapter.denormalize(data, local_env)

        market = data.get("market") or {}
        market_id = str(market.get("id") or data.get("name") or "script")
        file_name = f"market_{sanitize_market_id(market_id)}.json"
        path = self.scripts_dir / file_name
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(package, fh, ensure_ascii=False, indent=2)

        self._record_installed(market_id, str(market.get("version", "")), file_name)
        logger.info("已从市场导入脚本: %s -> %s", market_id, path)

        return {"file": str(path), "stem": path.stem, "issues": issues, "package": package}


class MarketExporter:
    """把本地脚本打包成可上传的 v2 脚本包。"""

    @staticmethod
    def build_package(script_path: Path, meta: Dict[str, Any],
                      env: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """读取本地脚本，补充 market/environment 元数据与归一化坐标。

        env 缺省时自动探测本机环境。
        """
        with open(script_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
            raise MarketValidationError("所选文件不是有效的动作脚本")

        if env is None:
            env = ScriptAdapter.detect_local_env()
        env = {k: v for k, v in env.items() if k in ENVIRONMENT_FIELDS and v is not None}

        market = {k: v for k, v in meta.items() if k in MARKET_FIELDS and v not in (None, "")}
        if not market.get("id"):
            raise MarketValidationError("上传需要填写脚本 id（唯一标识）")

        package: Dict[str, Any] = {
            "format_version": MARKET_FORMAT_VERSION,
            "market": market,
            "environment": env,
            "name": data.get("name") or script_path.stem,
            "actions": copy.deepcopy(data.get("actions", [])),
        }
        if env.get("resolution"):
            ScriptAdapter.normalize_actions(package["actions"], env["resolution"])

        SchemaValidator.validate_package(package)
        return package

    @staticmethod
    def export_to_file(package: Dict[str, Any], out_path: Path) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(package, fh, ensure_ascii=False, indent=2)
        return out_path
