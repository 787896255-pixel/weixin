# Windows 微信连接更新 0.6.1

日期：2026-10-23，Asia/Shanghai。

## 本次完成

用户发来 Hopper 许可证窗口截图。进入官方 Try the Demo 后，日志明确报告 Python 库不存在：初始配置指向 Xcode 的 Python 3.9 路径。选取内置 Python 后，自动检测又返回不存在的 `/install/lib/libpython3.12.dylib`。最终使用本机已存在的 Apple Command Line Tools Python 库，未安装新的运行环境。

当前 Hopper `PythonPath`：

```
/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Python3
```

修改前的偏好备份：`.build-cache/qx-research/hopper-preferences-before-python-20261023-001026.plist`。REA 和 Codex 的原有备份路径见《京客导出助手-功能对应与证据》。后续原生 CLI 已成功返回 Evidence，并正常结束；Hopper 的 Python 命令栏恢复可用。

先前试用到时的文档已经通过 UI 关闭，修复期间创建的临时文档也通过 UI 关闭，未保存或修改原程序。保留 `/tmp/rea-C2goVG`、`/tmp/rea-Fh2ylN` 作为排障材料，未擅自删除。新会话由 REA 自行恢复已经失去进程所有者的租约。回执：`.build-cache/qx-research/hopper-cleanup-20261023.json`。

## 读取实现的变化

- `wechatbrain/capture_v4.js`：新增独立实现的数据库入口捕获。在本人微信的 `Weixin.dll` 可执行区匹配两种观察到的 x64 指令特征；调用入口的第二个参数指向一个描述符，`+0x10` 处的 32 位长度必须为 32，`+8` 处为数据指针。无效或变化中的指针被忽略。
- 原 SHA-512 方式继续保留；两条路径可以同时工作。任一路径无法安装时，只要另一条已连接，就继续等待。候选去重后通过本地 Frida IPC 进入 Python，由所选数据库的 SQLite 头部和 HMAC 校验确认。
- `wechatbrain/windows_capture.py`：取消只取两个加密库首页、匹配后提前停止的限制。现在收集所选库的完整首页，按页面摘要去重，逐个验证未匹配的库，直至全部匹配、等待期限结束或用户取消。不同库可以使用不同的候选材料。
- 保留用户主动点击连接、手动重开并登录微信的流程；限定当前 Windows 用户的 `Weixin.exe`，目标需为 x64。结束时显式卸载脚本、解除连接。
- `app.py`、`web/app.js`：诊断显示连接方法、入口数量、进程数量、候选数量、已校验库数 / 总库数，以及队列溢出或连接异常。复制诊断只包含允许的公开字段。
- 版本更新到 0.6.1，附新版 Windows 便携包与源码包。

候选材料和有效连接信息只保存在本次运行内存中。本实现未使用原厂 DLL、全局 Windows 消息钩子、原厂接收窗口或缓存文件，也不含原厂授权机制。

## 静态证据

主文件与提取方法、原始偏移、长度、散列已保存在 `.build-cache/qx-research/native/inventory.json`。内嵌 PE 的 SHA-256：

```
06315396c0b6e1aa1c8aa4887384303e2a67a3c036586769243eadb1fabcb5b6
```

这个文件最初只是未确认用途的 PE 候选。本轮通过导出名、字符串、调用关系与指令读取进一步辨认它的捕获流程。下列内容是反编译器的静态表示，并非原始源码，也没有证明该代码在用户的微信版本中实际运行。

| 内容 | 地址 / 返回内容 | REA Evidence |
| --- | --- | --- |
| 概览 | x86_64、11 个段、51 个过程 | `ev_df03340fbcedeaa490de54fd321385a9822963b29273e6231cef28c9284b1ac8` |
| 导出入口 | `0x309c82920`，REA 名称 `exp_HookApp`，调用 SetWindowsHookExW | `ev_adbb233e62880197949fc6cefc359271d66e8fe07a1c65db6c1580bc5be25c67` |
| 进程筛选 | `0x309c82360`，比较 Weixin.exe / WeChat.exe | `ev_c48fbfdc93493608873a2547f7a806f78bd2e020fb191534cf86bbfa4d182f99` |
| 工作线程 | `0x309c81e50`，等待模块、定位目标并安装调用拦截 | `ev_b4e70e9ee457b1db5648b73a69487ae93e7df96df5710e038191e4deefb178cd` |
| 特征扫描 | `0x309c81340`，比较两组各 41 字节的指令特征 | `ev_dc09bf5caeb10586ea810462e5e9248832960140c6a4788575aaf2cdc8557f52` |
| 入口分类 | `0x309c81070`，检查两种 x64 函数开头 | `ev_745e7a2f39d552be89b73778cef668519603dd64d10074afac73c73bd7d4fef3` |
| 候选提取 | `0x309c826c0`，读描述符长度与指针、复制 32 字节、去重并发送 | `ev_9965e9279cc2830c2387c287f78dd093df6be853e150107c6274088aca5c4220` |

完整 REA JSON 留在 `.build-cache/qx-research/native/`，不进入发行包。对未标记为过程的 `0x309c81000`，REA 明确返回无过程；另用本机已有 `llvm-objdump` 读取该小段指令，观察到它调用 `0x309c826c0`。这个补充是普通工具输出，不能写成新的 REA Evidence。

特征字节来自原 PE 的文件映射并与扫描过程引用的位置核对。本项目用这些入口观察独立写出 Frida 连接逻辑，没有复制原 DLL 的安装钩子、汇编跳板、接收端或缓存代码。

## 检查与仍未完成的部分

本轮 Python 源文件语法编译、两份 JavaScript 的 `node --check` 均通过，已生成 `dist/JingkeExporter-0.6.1-Windows-x64.zip` 与源码包，没有新增或运行自动测试。语法检查和构建结果应与实际读取严格区分。

- Hopper 自动分析连接已恢复，并实际返回上述原生 Evidence。
- 微信 **4.1.15.55 的真实进程连接、候选验证、聊天导入仍未验收**；该版本的入口可能与这两种静态特征不同。
- 本轮没有在真实微信进程中执行新增脚本，也没有取得或输出用户密钥、聊天或 AI 结果。
- 本轮通过只读 PowerShell 列出当前可访问的 Parallels Windows 11 虚拟机进程，Weixin / WeChat 主进程计数为 0；这只代表该虚拟机此时的进程状态，不能推断另一台原 Windows 电脑的登录状态。回执保存在 `.build-cache/qx-research/windows-process-inventory-20261023.json`。
- 0.6.1 便携包未在 Windows 启动；0.6.0 的合成数据窗口截图不能作为 0.6.1 的读取结果。
- 加密 WAL、专用备份包、完整媒体等已有兼容性边界继续见 README。用户取消或只匹配部分库时，不能声称全部记录已经读取。
- 原主程序 Go 读取链路、其他嵌入组件、全部兼容版本和完整 UI 行为仍未穷尽分析，不能称为完美复刻。

Windows 使用：解压 0.6.1，双击 `启动京客导出助手.cmd`，选原记录文件夹并读取。仍有加密库未读出时，在处理结果点「连接微信 4.x（需手动重开）」，保持助手打开，手动重开并登录微信，查看实际“校验 X / Y”与导入统计。
