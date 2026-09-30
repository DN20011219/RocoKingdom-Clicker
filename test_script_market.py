"""脚本市场功能验证测试

覆盖实施计划中的测试项：
1. v2 包分辨率换算（导入时换算 / 运行时换算）
2. 非法包拒绝导入（未知动作类型、超深嵌套、超大文件、危险组合键、未知字段）
3. 索引不可达时降级（返回 None，不抛异常）
4. v1 旧脚本回归（加载行为不变）
5. 上传导出 → 导入回读往返无损
"""

import json
import shutil
import tempfile
from pathlib import Path

from ScriptMarket import (
    MAX_SCRIPT_BYTES,
    MarketError,
    MarketExporter,
    MarketImporter,
    MarketIndexClient,
    MarketValidationError,
    SchemaValidator,
    ScriptAdapter,
    compare_versions,
    sanitize_market_id,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def expect_error(name: str, fn, exc_type=MarketValidationError):
    try:
        fn()
    except exc_type:
        check(name, True)
    except Exception as e:
        check(name, False, f"(异常类型错误: {type(e).__name__}: {e})")
    else:
        check(name, False, "(未抛出异常)")


def make_sample_pkg() -> dict:
    return {
        "format_version": 2,
        "market": {"id": "roco.test.sample", "title": "测试脚本", "author": "tester",
                   "version": "1.0.0", "game": "RocoKingdom", "tags": ["test"],
                   "description": "用于测试"},
        "environment": {"resolution": [1920, 1080], "pointer_speed": 6,
                        "mouse_acceleration": False},
        "name": "sample",
        "actions": [
            {"type": "click", "x": 960, "y": 540, "x_norm": 0.5, "y_norm": 0.5,
             "hold_ms": 100},
            {"type": "loop", "count": 2, "actions": [
                {"type": "move", "x": 1919, "y": 1079, "x_norm": 1.0, "y_norm": 1.0,
                 "duration_ms": 50},
                {"type": "wait", "duration_ms": 10},
            ]},
        ],
    }


def test_denormalize():
    print("\n[1] 分辨率换算（导入时换算模式）")
    pkg = make_sample_pkg()
    local_env = {"resolution": [2560, 1440]}
    result = ScriptAdapter.denormalize(pkg, local_env)
    click = result["actions"][0]
    move = result["actions"][1]["actions"][0]
    check("中心点换算 x=1280", click["x"] == 1280, f"got {click['x']}")
    check("中心点换算 y=720", click["y"] == 720, f"got {click['y']}")
    check("右下角换算 x=2559", move["x"] == 2559, f"got {move['x']}")
    check("右下角换算 y=1439", move["y"] == 1439, f"got {move['y']}")
    check("换算后 runtime_normalize=False", result.get("runtime_normalize") is False)
    check("原包未被修改", pkg["actions"][0]["x"] == 960)


def test_import(tmp: Path):
    print("\n[2] 导入流程（落盘 + 已安装记录）")
    importer = MarketImporter(tmp / "scripts", tmp / "configs")
    raw = json.dumps(make_sample_pkg(), ensure_ascii=False).encode("utf-8")

    result = importer.import_raw(raw, mode="denormalize")
    file_path = Path(result["file"])
    check("落盘文件带 market_ 前缀", file_path.name.startswith("market_"),
          file_path.name)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    check("落盘内容通过校验", SchemaValidator.validate_package(data) is None)

    result_rt = importer.import_raw(raw, mode="runtime")
    data_rt = json.loads(Path(result_rt["file"]).read_text(encoding="utf-8"))
    check("运行时模式保留归一化坐标",
          data_rt.get("runtime_normalize") is True
          and data_rt["actions"][0].get("x_norm") == 0.5)

    installed = importer.get_installed()
    check("已安装记录写入", "roco.test.sample" in installed)
    check("更新检查：同版本 up_to_date",
          importer.installed_status({"id": "roco.test.sample", "version": "1.0.0"})
          == "up_to_date")
    check("更新检查：新版本 update_available",
          importer.installed_status({"id": "roco.test.sample", "version": "1.2.0"})
          == "update_available")
    check("更新检查：未安装 not_installed",
          importer.installed_status({"id": "other", "version": "1.0"})
          == "not_installed")


def test_validator_rejects():
    print("\n[3] 非法包拒绝")
    def pkg_with(actions=None, **overrides):
        pkg = make_sample_pkg()
        if actions is not None:
            pkg["actions"] = actions
        pkg.update(overrides)
        return pkg

    expect_error("未知动作类型",
                 lambda: SchemaValidator.validate_package(
                     pkg_with(actions=[{"type": "hack", "cmd": "rm"}])))
    expect_error("未知顶层字段",
                 lambda: SchemaValidator.validate_package(
                     pkg_with(executable="evil.exe")))
    expect_error("动作未知字段",
                 lambda: SchemaValidator.validate_package(
                     pkg_with(actions=[{"type": "wait", "duration_ms": 10,
                                        "shell": True}])))
    expect_error("x_norm 超出 0~1",
                 lambda: SchemaValidator.validate_package(
                     pkg_with(actions=[{"type": "click", "x": 1, "y": 1,
                                        "x_norm": 2.0, "y_norm": 0.5}])))
    expect_error("危险组合键 Win+L",
                 lambda: SchemaValidator.validate_package(
                     pkg_with(actions=[{"type": "combo", "vk_codes": [0x5B, 0x4C]}])))
    expect_error("vk_code 超范围",
                 lambda: SchemaValidator.validate_package(
                     pkg_with(actions=[{"type": "key", "vk_code": 999}])))

    deep = {"type": "wait", "duration_ms": 1}
    for _ in range(10):
        deep = {"type": "loop", "count": 1, "actions": [deep]}
    expect_error("嵌套深度超限",
                 lambda: SchemaValidator.validate_package(pkg_with(actions=[deep])))

    importer = MarketImporter(Path(tempfile.mkdtemp()) / "scripts",
                              Path(tempfile.mkdtemp()) / "configs")
    oversize = json.dumps(make_sample_pkg()).encode() + b" " * MAX_SCRIPT_BYTES
    expect_error("超大文件拒绝", lambda: importer.import_raw(oversize))
    expect_error("非法 JSON 拒绝", lambda: importer.import_raw(b"{broken"))


def test_index_client():
    print("\n[4] 索引客户端降级")
    check("空 URL 返回 None", MarketIndexClient("").fetch_index() is None)
    check("不可达地址返回 None（不抛异常）",
          MarketIndexClient("https://invalid.invalid/index.json",
                            timeout=2).fetch_index() is None)
    expect_error("非 http 协议拒绝",
                 lambda: MarketIndexClient("file:///etc/passwd")
                 .download_script("file:///etc/passwd"))


def test_v1_regression(tmp: Path):
    print("\n[5] v1 旧脚本回归")
    from ActionScript import ActionScriptManager, ClickAction, LoopAction

    manager = ActionScriptManager(tmp)
    scripts_dir = tmp / "action_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    v1 = {
        "name": "legacy",
        "actions": [
            {"type": "click", "x": 900, "y": 500, "hold_ms": 80},
            {"type": "loop", "count": 3, "actions": [
                {"type": "move", "x": 100, "y": 100, "duration_ms": 50},
            ]},
        ],
    }
    (scripts_dir / "legacy.json").write_text(
        json.dumps(v1, ensure_ascii=False), encoding="utf-8")
    actions = manager.load_script("legacy")
    check("v1 加载动作数量", len(actions) == 2, f"got {len(actions)}")
    check("v1 坐标不变", isinstance(actions[0], ClickAction)
          and actions[0].x == 900 and actions[0].y == 500)
    check("v1 无归一化字段", actions[0].x_norm is None
          and actions[0].runtime_normalize is False)
    inner = actions[1].actions[0] if isinstance(actions[1], LoopAction) else None
    check("v1 嵌套动作正常", inner is not None and inner.x == 100)
    check("v1 列入脚本列表", "legacy" in manager.list_scripts())


def test_runtime_normalize_loading(tmp: Path):
    print("\n[6] 运行时归一化加载")
    from ActionScript import ActionScriptManager

    importer = MarketImporter(tmp / "action_scripts", tmp / "configs")
    raw = json.dumps(make_sample_pkg(), ensure_ascii=False).encode("utf-8")
    result = importer.import_raw(raw, mode="runtime")

    manager = ActionScriptManager(tmp)
    actions = manager.load_script(result["stem"])
    check("运行时模式动作数量", len(actions) == 2, f"got {len(actions)}")
    check("点击动作带归一化坐标",
          actions[0].runtime_normalize is True and actions[0].x_norm == 0.5)

    base_x, base_y = None, None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        w = max(user32.GetSystemMetrics(0) - 1, 1)
        h = max(user32.GetSystemMetrics(1) - 1, 1)
        base_x, base_y = int(round(0.5 * w)), int(round(0.5 * h))
    except Exception:
        pass
    if base_x is not None:
        from ActionScript import ActionExecutor
        executor = ActionExecutor.__new__(ActionExecutor)  # 不初始化 Interception
        resolved = executor._resolve_base_xy(actions[0])
        check("运行时动态换算坐标", resolved == (base_x, base_y), f"got {resolved}")


def test_export_roundtrip(tmp: Path):
    print("\n[7] 上传导出 → 导入往返")
    scripts_dir = tmp / "action_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    local_script = {
        "name": "my_script",
        "actions": [
            {"type": "click", "x": 640, "y": 360, "hold_ms": 90},
            {"type": "timed", "execute_ms": 1000, "sleep_ms": 500, "forever": True,
             "actions": [{"type": "move", "x": 100, "y": 100, "duration_ms": 60}]},
        ],
    }
    script_path = scripts_dir / "my_script.json"
    script_path.write_text(json.dumps(local_script), encoding="utf-8")

    env = {"resolution": [1280, 720], "pointer_speed": 6, "mouse_acceleration": False}
    meta = {"id": "roco.my.script", "title": "我的脚本", "author": "me",
            "version": "1.0.0", "game": "RocoKingdom", "tags": ["demo"],
            "description": "往返测试"}
    package = MarketExporter.build_package(script_path, meta, env=env)
    click = package["actions"][0]
    check("导出生成归一化坐标",
          abs(click["x_norm"] - 640 / 1279) < 1e-4
          and abs(click["y_norm"] - 360 / 719) < 1e-4,
          f"got {click.get('x_norm')}, {click.get('y_norm')}")
    check("嵌套 timed 内动作也归一化",
          "x_norm" in package["actions"][1]["actions"][0])
    check("导出包含环境与元数据",
          package["environment"]["resolution"] == [1280, 720]
          and package["market"]["id"] == "roco.my.script")

    out_path = tmp / "upload" / "market_my.json"
    MarketExporter.export_to_file(package, out_path)
    raw = out_path.read_bytes()

    importer = MarketImporter(tmp / "market_scripts", tmp / "configs2")
    result = importer.import_raw(raw, mode="denormalize")
    imported = json.loads(Path(result["file"]).read_text(encoding="utf-8"))
    check("往返后动作结构完整",
          len(imported["actions"]) == 2
          and imported["actions"][1]["type"] == "timed")
    check("往返后通过校验",
          SchemaValidator.validate_package(imported) is None)
    expect_error("缺少 id 拒绝导出",
                 lambda: MarketExporter.build_package(
                     script_path, {"title": "no id"}, env=env))


def test_misc():
    print("\n[8] 工具函数与环境比对")
    check("版本号比较", compare_versions("1.0.0", "1.2.0") == -1
          and compare_versions("1.10.0", "1.9.9") == 1
          and compare_versions("2.0", "2.0.0") == 0)
    check("id 规范化", sanitize_market_id("a/b\\c:d") == "a_b_c_d")

    issues = ScriptAdapter.compare_env(
        {"resolution": [1920, 1080], "pointer_speed": 6, "mouse_acceleration": False},
        {"resolution": [2560, 1440], "pointer_speed": 11, "mouse_acceleration": True})
    levels_msgs = issues
    check("分辨率不一致产生警告",
          any(lv == "warn" and "分辨率" in msg for lv, msg in levels_msgs))
    check("鼠标加速不一致产生警告",
          any(lv == "warn" and "鼠标加速" in msg or "提高指针精确度" in msg
              for lv, msg in levels_msgs))
    check("指针速度不一致仅提示",
          any(lv == "info" and "指针速度" in msg for lv, msg in levels_msgs))

    same = ScriptAdapter.compare_env(
        {"resolution": [1920, 1080]}, {"resolution": [1920, 1080]})
    check("分辨率一致提示 info",
          any(lv == "info" and "一致" in msg for lv, msg in same))

    dx, dy = ScriptAdapter.scale_relative(100, 50, {"pointer_speed": 6},
                                          {"pointer_speed": 11})
    check("相对位移缩放预留接口", abs(dx - 100 * 11 / 6) < 1e-6 and
          abs(dy - 50 * 11 / 6) < 1e-6)

    local_env = ScriptAdapter.detect_local_env()
    check("本机环境探测", isinstance(local_env["resolution"], list)
          and local_env["resolution"][0] > 0)


def main():
    tmp_root = Path(tempfile.mkdtemp(prefix="market_test_"))
    try:
        test_denormalize()
        test_import(tmp_root / "t2")
        test_validator_rejects()
        test_index_client()
        test_v1_regression(tmp_root / "t5")
        test_runtime_normalize_loading(tmp_root / "t6")
        test_export_roundtrip(tmp_root / "t7")
        test_misc()
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
