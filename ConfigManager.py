"""
连点器配置管理模块 - Interception 版
支持从文件加载和保存配置
"""

import json
import logging
import sys
from pathlib import Path

from InterceptionCore import ClickerConfig


# F1-F12 虚拟键码映射
FKEY_VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}

# F1-F12 扫描码映射（AT Set 2，Interception 使用此套）
FKEY_SCANCODE = {
    "F1": 0x3B, "F2": 0x3C, "F3": 0x3D, "F4": 0x3E,
    "F5": 0x3F, "F6": 0x40, "F7": 0x41, "F8": 0x42,
    "F9": 0x43, "F10": 0x44, "F11": 0x57, "F12": 0x58,
}

# 默认热键配置
DEFAULT_HOTKEYS = {
    "pause_resume": "F2",       # 暂停/继续脚本和回放
    "start_recording": "F7",    # 开始录制
    "stop_recording": "F8",     # 停止录制并保存
    "cancel_recording": "F9",   # 取消录制
    "mark_anchor": "F12",       # 录制时标记锚点
}

# 默认锚点模式："point"（点锚点，路径扰动）或 "segment"（段锚点，区间保护）
DEFAULT_ANCHOR_MODE = "point"

# 默认路径扰动配置
DEFAULT_PATH_PLANNER = {
    "strategy": "fitts",
    # sine 策略参数
    "sine_amplitude_px": 10.0,
    "sine_frequency": 2,
    # fitts 策略参数
    "fitts_jitter_px": 2.0,
    "fitts_arc_px": 8.0,
    "fitts_overshoot_chance": 0.3,
    "fitts_overshoot_px": 15.0,
    # neuromotor 策略参数
    "nm_lognormal_sigma": 0.65,
    "nm_perception_noise": 0.03,
    "nm_entropy_alpha": 0.6,
    "nm_lateral_drift": 0.06,
    "nm_max_corrections": 2,
}

# 热键显示名称
HOTKEY_LABELS = {
    "pause_resume": "暂停/继续",
    "start_recording": "开始录制",
    "stop_recording": "停止录制并保存",
    "cancel_recording": "取消录制",
    "mark_anchor": "标记锚点（录制中）",
}


class ConfigManager:
    """配置管理类"""

    logger = logging.getLogger("clicker")

    if getattr(sys, "frozen", False):
        BASE_DIR = Path(sys.executable).resolve().parent
    else:
        BASE_DIR = Path(__file__).resolve().parent

    CONFIG_DIR = BASE_DIR / "data" / "clicker_configs"
    DEFAULT_CONFIG_FILE = CONFIG_DIR / "default.json"
    HOTKEYS_FILE = CONFIG_DIR / "hotkeys.json"

    def __init__(self, base_dir: Path | None = None):
        """兼容 click/ 项目的实例化调用。"""
        if base_dir is not None:
            self.BASE_DIR = Path(base_dir).resolve()
            self.CONFIG_DIR = self.BASE_DIR / "clicker_configs"
            self.DEFAULT_CONFIG_FILE = self.CONFIG_DIR / "default.json"
    
    # 预设配置
    PRESETS = {
        'fast': {
            'name': '快速模式（高频率）',
            'click_interval': 50,
            'hold_duration': 30,
            'radius': 20,
            'jitter_range': 10,
        },
        'balanced': {
            'name': '平衡模式（推荐）',
            'click_interval': 100,
            'hold_duration': 50,
            'radius': 30,
            'jitter_range': 20,
        },
        'stable': {
            'name': '稳定模式（低调）',
            'click_interval': 150,
            'hold_duration': 80,
            'radius': 40,
            'jitter_range': 30,
        },
        'custom': {
            'name': '自定义模式',
            'click_interval': 100,
            'hold_duration': 50,
            'radius': 30,
            'jitter_range': 20,
        }
    }
    
    @classmethod
    def ensure_config_dir(cls):
        """确保配置目录存在"""
        cls.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    @classmethod
    def load_config(cls, config_name: str = 'default') -> ClickerConfig:
        """
        从文件加载配置
        
        Args:
            config_name: 配置文件名（不含.json后缀）
        
        Returns:
            ClickerConfig 对象
        """
        cls.ensure_config_dir()
        config_file = cls.CONFIG_DIR / f"{config_name}.json"
        
        config = ClickerConfig()
        
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                if 'center_x' in data:
                    config.center_x = data['center_x']
                if 'center_y' in data:
                    config.center_y = data['center_y']
                if 'radius' in data:
                    config.radius = data['radius']
                if 'move_mouse' in data:
                    config.move_mouse = bool(data['move_mouse'])
                if 'click_interval' in data:
                    config.click_interval = data['click_interval']
                if 'hold_duration' in data:
                    config.hold_duration = data['hold_duration']
                if 'jitter_range' in data:
                    config.jitter_range = data['jitter_range']
                cls.logger.info("配置已从 %s 加载", config_file)
            except Exception as e:
                cls.logger.warning("加载配置失败: %s，使用默认配置", e)
        else:
            cls.logger.info("配置文件 %s 不存在，使用默认配置", config_file)
        
        return config
    
    @classmethod
    def save_config(cls, config_data, config_name: str = 'default'):
        """
        保存配置到文件
        
        Args:
            config_data: 配置对象或配置字典
            config_name: 配置文件名（不含.json后缀）
        """
        cls.ensure_config_dir()
        config_file = cls.CONFIG_DIR / f"{config_name}.json"
        
        try:
            if hasattr(config_data, "center_x"):
                data = {
                    'center_x': config_data.center_x,
                    'center_y': config_data.center_y,
                    'radius': config_data.radius,
                        'move_mouse': getattr(config_data, 'move_mouse', True),
                    'click_interval': config_data.click_interval,
                    'hold_duration': config_data.hold_duration,
                    'jitter_range': config_data.jitter_range,
                }
            else:
                data = dict(config_data)

            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            
            cls.logger.info("配置已保存到 %s", config_file)
        except Exception as e:
            cls.logger.error("保存配置失败: %s", e)
    
    @classmethod
    def load_preset(cls, preset_name: str) -> ClickerConfig:
        """
        加载预设配置
        
        Args:
            preset_name: 预设名称 ('fast', 'balanced', 'stable', 'custom')
        
        Returns:
            ClickerConfig 对象
        """
        config = ClickerConfig()
        
        if preset_name not in cls.PRESETS:
            cls.logger.warning("预设 '%s' 不存在", preset_name)
            return config
        
        preset = cls.PRESETS[preset_name]
        config.click_interval = preset.get('click_interval', config.click_interval)
        config.hold_duration = preset.get('hold_duration', config.hold_duration)
        config.radius = preset.get('radius', config.radius)
        config.jitter_range = preset.get('jitter_range', config.jitter_range)
        
        cls.logger.info("预设 '%s' 已加载", preset['name'])
        return config
    
    @classmethod
    def list_configs(cls) -> list:
        """
        列出所有保存的配置
        
        Returns:
            配置文件名列表
        """
        cls.ensure_config_dir()
        configs = []
        
        for f in cls.CONFIG_DIR.glob("*.json"):
            configs.append(f.stem)
        
        return sorted(configs)
    
    @classmethod
    def list_presets(cls) -> list:
        """
        列出所有预设
        
        Returns:
            预设列表
        """
        presets = []
        for name, info in cls.PRESETS.items():
            presets.append((name, info['name']))
        return presets
    
    @classmethod
    def delete_config(cls, config_name: str) -> bool:
        """
        删除配置文件

        Args:
            config_name: 配置文件名

        Returns:
            是否删除成功
        """
        cls.ensure_config_dir()
        config_file = cls.CONFIG_DIR / f"{config_name}.json"

        if config_file.exists():
            try:
                config_file.unlink()
                cls.logger.info("配置已删除: %s", config_file)
                return True
            except Exception as e:
                cls.logger.error("删除配置失败: %s", e)
                return False
        else:
            cls.logger.warning("配置文件不存在: %s", config_file)
            return False

    # ---- 热键配置 ----

    @classmethod
    def load_hotkeys(cls) -> dict:
        """加载热键配置，返回 {key_name: fkey_str} 字典。"""
        cls.ensure_config_dir()
        hotkeys = dict(DEFAULT_HOTKEYS)
        if cls.HOTKEYS_FILE.exists():
            try:
                with open(cls.HOTKEYS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for key in DEFAULT_HOTKEYS:
                    val = data.get(key)
                    if val and val in FKEY_VK:
                        hotkeys[key] = val
                cls.logger.info("热键配置已从 %s 加载", cls.HOTKEYS_FILE)
            except Exception as e:
                cls.logger.warning("加载热键配置失败: %s，使用默认配置", e)
        else:
            cls.logger.info("热键配置文件不存在，使用默认配置")
        return hotkeys

    @classmethod
    def save_hotkeys(cls, hotkeys: dict) -> bool:
        """保存热键配置到文件。"""
        cls.ensure_config_dir()
        try:
            # 只保存已知的键，且值必须是合法的 F-key
            data = {}
            for key in DEFAULT_HOTKEYS:
                val = hotkeys.get(key, DEFAULT_HOTKEYS[key])
                if val in FKEY_VK:
                    data[key] = val
            with open(cls.HOTKEYS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            cls.logger.info("热键配置已保存到 %s", cls.HOTKEYS_FILE)
            return True
        except Exception as e:
            cls.logger.error("保存热键配置失败: %s", e)
            return False

    # ---- 锚点模式配置 ----

    @classmethod
    def load_anchor_mode(cls) -> str:
        """加载锚点模式配置，返回 'point' 或 'segment'。"""
        cls.ensure_config_dir()
        if cls.HOTKEYS_FILE.exists():
            try:
                with open(cls.HOTKEYS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                mode = data.get("anchor_mode", DEFAULT_ANCHOR_MODE)
                if mode in ("point", "segment"):
                    return mode
            except Exception as e:
                cls.logger.warning("加载锚点模式失败: %s，使用默认配置", e)
        return DEFAULT_ANCHOR_MODE

    @classmethod
    def save_anchor_mode(cls, mode: str) -> bool:
        """保存锚点模式配置到 hotkeys.json。"""
        if mode not in ("point", "segment"):
            cls.logger.warning("无效的锚点模式: %s", mode)
            return False
        cls.ensure_config_dir()
        try:
            # 读取现有配置，追加/更新 anchor_mode 字段
            data = {}
            if cls.HOTKEYS_FILE.exists():
                with open(cls.HOTKEYS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            data["anchor_mode"] = mode
            with open(cls.HOTKEYS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            cls.logger.info("锚点模式已保存: %s", mode)
            return True
        except Exception as e:
            cls.logger.error("保存锚点模式失败: %s", e)
            return False

    # ---- 路径扰动算法配置 ----

    PATH_PLANNER_FILE = CONFIG_DIR / "path_planner.json"

    @classmethod
    def load_path_planner(cls) -> dict:
        """加载路径扰动算法配置，返回完整参数字典。"""
        cls.ensure_config_dir()
        config = dict(DEFAULT_PATH_PLANNER)
        if cls.PATH_PLANNER_FILE.exists():
            try:
                with open(cls.PATH_PLANNER_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for key in DEFAULT_PATH_PLANNER:
                    if key in data:
                        config[key] = data[key]
                cls.logger.info("路径扰动配置已从 %s 加载", cls.PATH_PLANNER_FILE)
            except Exception as e:
                cls.logger.warning("加载路径扰动配置失败: %s，使用默认配置", e)
        return config

    @classmethod
    def save_path_planner(cls, config: dict) -> bool:
        """保存路径扰动算法配置到文件。"""
        cls.ensure_config_dir()
        try:
            # 只保存已知字段
            data = {}
            for key in DEFAULT_PATH_PLANNER:
                if key in config:
                    data[key] = config[key]
            with open(cls.PATH_PLANNER_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            cls.logger.info("路径扰动配置已保存到 %s", cls.PATH_PLANNER_FILE)
            return True
        except Exception as e:
            cls.logger.error("保存路径扰动配置失败: %s", e)
            return False
