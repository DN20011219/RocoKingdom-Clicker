# RocoKingdom Clicker

在洛克王国：世界游戏中，**大量出没活动**刷新的精灵点位是固定的，可以通过不移动位置连续扔球实现捕捉，但这个过程非常乏味且单一。

因此，可以通过 “寻找一个安全的站位 -> 连续触发捕捉动作” 来简化这个过程的操作成本，本项目 **洛克王国连点器** 就是为了实现这个目的创建的。

需要说明的是，本项目为本人为学习编程而编写的项目，无任何商业盈利行为，可能涉及到官方封禁的风险，请酌情使用。若账号被封禁，本人无任何责任。

## 你需要做什么

需要首先安装 python 环境，接着：

1. 先按 [DRIVER_INSTALLATION.md](./docs/DRIVER_INSTALLATION.md) 安装 Interception 驱动，将编译得到的 DLL 复制到本项目的根目录，然后重启电脑
2. 双击 `run_clicker.bat` 启动程序，然后输入数字 0 并回车，进入连点器监控页面
3. 进入游戏
4. 在游戏内，按 `F1` 启动连点器（会同时开启赛季页面，立刻按`ESC`退出）
5. 3秒后连点开始
6. 想停止时，在游戏内按 `F2` 即可

热键监听现在使用的是全局键盘钩子，只记录按键事件，不会拦截按键本身，所以游戏原本的热键功能仍然会保留。

## 启动方式

推荐直接运行：

```bat
run_clicker.bat
```

这个脚本会自动申请管理员权限，并根据当前目录自动选择运行模式：

- 有 `RocoKingdom_Clicker.exe` 时，直接启动发布包
- 没有 `RocoKingdom_Clicker.exe` 时，创建本地 `.venv` 并启动 `Clicker.py`

## 发布打包

如果你要生成发布包，直接运行根目录下的 `build_release.bat` 即可：

```bat
.\build_release.bat
```

打包脚本会自动探测可用的全局 Python，安装 PyInstaller，执行 `onedir` 打包，并把 `interception.dll`、`run_clicker.bat`、`README.md` 以及下面这些运行数据一并放进发布目录，最后生成：

- `release/RocoKingdom_Clicker.zip`

另外，发布包会明确带上下面这些运行数据，方便直接使用示例脚本和默认配置：

- `data/action_scripts/`
- `data/clicker_configs/default.json`

脚本默认会在可用的 Python 解释器里按以下顺序探测：

1. `py -3.10`
2. `py -3`
3. `python`
4. `%LocalAppData%\Programs\Python\Python310\python.exe`

## 项目文件

- `Clicker.py`：主程序和菜单
- `InterceptionCore.py`：点击核心
- `ActionScript.py`：动作脚本系统
- `ConfigManager.py`：配置保存与加载
- `run_clicker.bat`：正式启动脚本
- `build_release.bat`：正式发布打包脚本

## 多步动作脚本

脚本现在支持多步动作，不再局限于单个点击。你可以在菜单里进入“动作脚本管理”，查看、执行或删除脚本，但脚本内容需要直接在 `data/action_scripts/` 目录下编写。

### 使用方式

1. 启动程序后输入 `0`，进入热键监听前的主菜单。
2. 选择 `8` 打开“动作脚本管理”。
3. 直接在 `data/action_scripts/` 目录下编写或修改脚本 JSON 文件。
4. 按 `SCRIPT_RULES.md` 的规则组织动作序列。
5. 保存后就可以在脚本列表里选择执行，或直接用 `Delete+1~9` 触发对应脚本。

### 脚本编写位置

脚本不再通过菜单创建，而是直接编辑下面这个目录里的 JSON 文件：

- `data/action_scripts/script_1.json`
- `data/action_scripts/script_2.json`
- 其他自定义脚本文件

详细格式和约定请看 [SCRIPT_RULES.md](data/action_scripts/SCRIPT_RULES.md)。

### 脚本会话控制

选择脚本后，会进入脚本会话模式：

- `F2` 暂停脚本
- `F1` 继续脚本，继续前会再次等待 3 秒
- `F4` 退出脚本并返回菜单

开始执行或继续执行前，程序都会给出 3 秒倒计时，方便你切回游戏窗口。

### 支持的动作类型

- `click`：移动到指定坐标并点击一次（若全局设置 `move_mouse=false`，则不会移动鼠标，仅在当前鼠标位置发送按下/释放事件）
- `move`：移动到指定坐标，不点击
- `key`：按下并释放一个虚拟键码
- `combo`：同时按下多个虚拟键码，适合 WASD 转圈或斜向移动
- `wait`：暂停指定毫秒数
- `loop`：循环执行一组动作，`count=0` 或 `forever=true` 表示一直循环到按 `F4`

注意：程序默认不会在每次点击前移动鼠标（全局设置 `move_mouse` 默认为 false）。
当 `move_mouse=false` 时，`click` 动作只会发送按下/释放事件，作用于当前鼠标位置；
如需切换，可在主菜单按 `m` 切换，或编辑配置文件 `data/clicker_configs/default.json` 中的 `move_mouse` 字段。

### 类人化选项

脚本里可以额外加随机扰动字段，让动作更像真人输入：

- `x_jitter_px`、`y_jitter_px`：坐标抖动
- `hold_jitter_ms`：按住时间抖动
- `duration_jitter_ms`：持续时间抖动
- `pause_jitter_ms`：循环间隔抖动

### 示例脚本

脚本文件保存在 `data/action_scripts/` 目录下，文件名格式通常是 `script_1.json`、`script_2.json`。

如果你要一个直接可用的 WASD 转圈脚本，可以参考 [move_wasd_circle.json](data/action_scripts/move_wasd_circle.json)。

```json
{
	"name": "script_1",
	"actions": [
		{"type": "loop", "count": 0, "actions": [
			{"type": "move", "x": 900, "y": 500, "duration_ms": 120},
			{"type": "click", "x": 900, "y": 500, "hold_ms": 80},
			{"type": "wait", "duration_ms": 300}
		]}
	]
}
```

上面这个写法就是“把连点器直接写成一个脚本”的标准形式。只要把动作放进 `loop` 里，脚本就会一直执行，直到你按 `F4`。

如果你想限定次数，可以把 `count` 改成具体数字，比如 `count: 20` 表示循环 20 次。

WASD 转圈脚本推荐使用 `combo` 动作，例如 `W+D`、`D+S`、`S+A`、`A+W` 轮流执行，再配合 `hold_jitter_ms` 和 `pause_jitter_ms`。

### 适合什么场景

- 先移动到固定位置，再连续点击
- 点击后等待一段时间，再执行下一步
- 插入按键动作作为中间步骤
- 组合成重复的固定流程，减少手动操作
- 把整套连点流程封装成一个常驻脚本

## 鸣谢

[洛克王国：世界--MAA](https://github.com/krendluck/lkwg) - 该项目首次采用 Interception 作为游戏控制的输入，启发了本项目。

[Interception](https://github.com/oblitum/Interception) - 输入设备拦截与控制 API，本项目用于绕过游戏输入保护