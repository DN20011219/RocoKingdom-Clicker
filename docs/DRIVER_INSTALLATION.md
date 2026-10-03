# 安装 Interception 驱动

本程序靠 **Interception 驱动**（键鼠类过滤驱动）模拟鼠标点击，首次使用需要装一次驱动，装完**必须重启电脑**。

仓库在 `third/Interception/` 下已经提供了官方编译好的 `interception.dll`（x64 / x86 各一份）以及驱动安装工具，因此**不需要自行安装 Windows Driver Kit，也不需要自己编译 DLL**。

## 我该看哪一节？

| 你的情况 | 看这里 |
| --- | --- |
| 第一次用，只想把驱动装好 | [方式一：程序内一键安装](#方式一程序内一键安装推荐) |
| 不想先启动主程序 | [方式二：install_driver.bat](#方式二install_driverbat) |
| 自动安装失败，想自己敲命令 | [方式三：纯手动](#方式三纯手动) |
| 已经装完，想确认是否生效 | [验证](#验证) |
| 程序报错了 | [排查](#排查) |
| 维护这个仓库（改 `.bat` / 改检测逻辑） | [附录：开发者注意事项](#附录开发者注意事项) |

---

## 方式一：程序内一键安装（推荐）

直接双击运行 `RocoKingdom_Clicker.exe`。

如果检测到驱动没装好，程序会弹出对话框：

```
RocoKingdom Clicker 需要 Interception 驱动才能模拟鼠标点击。
检测到驱动【尚未安装】，是否现在自动安装？
```

点「**是**」后：

1. 可能弹一次 UAC 提权窗口 → 点「是」
2. 程序静默调用官方 `install-interception.exe /install`，约几秒钟
3. 装完再问「是否立即重启电脑」→ 选「是」会 15 秒倒计时重启（期间可用 `shutdown /a` 取消），选「否」则稍后自己重启

全程不需要打开终端、不需要敲任何命令。

> 发布包的 exe 由 PyInstaller `--uac-admin` 打包，已内嵌 `requireAdministrator` 清单，双击即提权，所以大多数情况下连 UAC 都只弹一次。

## 方式二：`install_driver.bat`

不想先启动主程序的话，双击包里的 `install_driver.bat`。它只是一个**启动器**，真正的安装流程与全部中文提示都来自 `DriverInstaller.py`，因此看到的对话框与方式一完全一致。

它按以下优先级选择执行方式：

| 你所在的环境 | 走哪条路 |
| --- | --- |
| 发布包（有 `RocoKingdom_Clicker.exe`） | `RocoKingdom_Clicker.exe --install-driver`；exe 自带提权清单，自己弹 UAC |
| 源码仓库（有 `DriverInstaller.py` 与 Python） | `python DriverInstaller.py --install` |
| 两者都没有 | 直接调官方 `install-interception.exe`，用 PowerShell `Start-Process -Verb RunAs` 提权 |

卸载驱动：

```bat
install_driver.bat /uninstall
```

### 直接用命令行（等价形式）

跳过 `.bat`，自己调下面任意一组命令，效果与上表一致：

```bat
RocoKingdom_Clicker.exe --install-driver
RocoKingdom_Clicker.exe --uninstall-driver

python DriverInstaller.py --install
python DriverInstaller.py --uninstall
python DriverInstaller.py --status
```

## 方式三：纯手动

### 1. 定位安装程序

| 你拿到的是 | 安装程序路径 |
| --- | --- |
| 发布包（release zip） | `driver_installer\install-interception.exe` |
| 源码仓库 | `third\Interception\command line installer\install-interception.exe` |

运行该程序**需要管理员权限**。

### 2. 以管理员权限执行安装

以**管理员权限**打开 CMD 或 PowerShell：

![以管理员权限打开终端](pic/admin_terminal.png)

进入安装程序所在目录后执行：

```bat
./install-interception.exe /install
```

看到以下输出说明安装成功：

```
Interception command line installation tool
Copyright (C) 2008-2018 Francisco Lopes da Silva

Interception successfully installed. You must reboot for it to take effect.
```

### 3. 重启电脑

安装驱动后，**必须重启系统**才能生效。

### 4. 卸载（如需）

同样以管理员权限进入同一目录执行：

```bat
./install-interception.exe /uninstall
```

然后重启系统。

---

## 验证

重启后双击 `RocoKingdom_Clicker.exe`（源码用户：`python Clicker.py --gui`）。能正常进入界面、状态栏显示 Interception 就绪，说明驱动与 DLL 都加载成功。

也可以查一次注册表状态（只读，不改系统，不需要管理员权限）：

```bat
python DriverInstaller.py --status
```

输出示例：

```
Interception 驱动状态：
  已安装（注册表痕迹）: True
    keyboard_filter    = True
    mouse_filter       = True
    keyboard_service   = True
    mouse_service      = True
    mouse_driver_file  = True
  当前进程管理员权限  : False
  安装器路径          : ...\third\Interception\command line installer\install-interception.exe
```

> `sc query interception` **永远查不到**这个驱动，那是正常的，不代表没装上。原因见[附录](#为什么不能用-sc-query-检测)。

## 排查

### 提示「找不到或无法加载 interception.dll」

这**不是驱动问题**，重装驱动修不好。原因是程序文件不完整：

1. 发布包用户：重新下载并**完整解压**到同一目录，确认 `interception.dll` 与 `RocoKingdom_Clicker.exe` 同级（不要在压缩包里直接双击运行）。
2. 源码用户：确认 `third\Interception\library\x64\interception.dll` 存在（64 位 Python 优先用它；32 位 Python 会自动尝试 `x86` 那份）。
3. 位数不匹配也会加载失败：32 位 Python 加载 64 位 DLL 会直接报错。

### 提示「无法创建上下文 / 驱动未就绪」

按可能性排序：

1. **装完还没重启** —— 最常见，重启即可。
2. 驱动确实没装成功 —— 重跑一次 `install_driver.bat`，看它打印的安装器输出。
3. 安全软件 / 反作弊拦下了驱动写入 —— 看安装器输出里有没有 `Access is denied` 之类。
4. Windows 11 25H2 开启了「内存完整性」（核心隔离）等驱动加固策略时，未签名的旧驱动可能被拒载。

### 提示「装了驱动但检测说没装」

先跑 `python DriverInstaller.py --status` 看五项注册表痕迹分别是什么。多数情况是安装后没有重启，重启即可；若 `mouse_filter` / `mouse_service` 本来就是 `False`，说明安装器确实没写进去，重跑一次方式二。

检测原理与注册表位置见[附录](#为什么不能用-sc-query-检测)。

### 提示「驱动已就绪，但没有发现任何鼠标设备」

说明驱动没问题，是 Interception 在鼠标槽位（11~20）里没找到挂着硬件的设备。确认鼠标 / 触摸板已连接并被系统识别，然后重新运行程序。这种情况**不需要**重装驱动。

---

## 附录：开发者注意事项

以下内容面向维护本仓库的人，普通用户不需要看。

### `.bat` 必须保持纯 ASCII

`build_release.bat` / `install_driver.bat` / `run_clicker.bat` 三个批处理文件**刻意不写中文**。

cmd.exe 按「字符数」而不是「字节数」推进它在批处理文件里的读取位置，含多字节字符的行会让它停在行中间，把行尾当成新命令执行。实测 Windows 11 25H2：一行纯中文的 `rem` 注释会报 `'venv' is not recognized as an internal or external command`，尽管它只是注释。

要新增面向用户的提示，请加到 `DriverInstaller.py` 的消息框里，不要加到 `.bat` 的 `echo`。

### 为什么不能用 `sc query` 检测

Interception 以键盘/鼠标**类过滤驱动**形式安装，服务名就叫 `keyboard` 和 `mouse`，并没有名为 `interception` 的服务。所以 `sc query interception` 永远返回 1060（假阴性）。

`DriverInstaller.py` 改用三个可靠信号（读 HKLM，不需要管理员权限）：

| 信号 | 位置 |
| --- | --- |
| 类键 `UpperFilters` 含 `mouse` / `keyboard` | `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E96F-E325-11CE-BFC1-08002BE10318}`（鼠标类）<br>`HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E96B-E325-11CE-BFC1-08002BE10318}`（键盘类） |
| 服务键存在 | `HKLM\SYSTEM\CurrentControlSet\Services\mouse`（及 `keyboard`） |
| 驱动文件落地 | `%SystemRoot%\System32\drivers\mouse.sys` |

装好后前两项应分别为 `mouse\0mouclass` / `keyboard\0kbdclass`。

注意 `is_driver_installed()` 只能说明「装过」，不代表「已生效」—— 安装后必须重启。真正能否使用由 `InterceptionCore.is_ready()` 决定。
