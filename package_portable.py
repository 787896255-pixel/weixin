"""在任意开发系统组装 Windows x64 便携版，不假称完成 Windows 运行验收。

准备：官方 Python embeddable zip + pip 下载的 win_amd64 wheels。
"""
from pathlib import Path
import hashlib
import json
import shutil
import zipfile

ROOT=Path(__file__).resolve().parent
VERSION='0.14.0'
PYTHON='3.13.17'


def build():
    output=ROOT/'dist'/f'JingkeExporter-{VERSION}-Windows-x64'
    if output.exists():shutil.rmtree(output)
    runtime=output/'runtime';runtime.mkdir(parents=True)
    python_zip=ROOT/'.build-cache'/f'python-{PYTHON}-embed-amd64.zip'
    wheels=sorted((ROOT/'.build-cache/windows-wheels').glob('*.whl'))
    if not python_zip.is_file() or not wheels:raise SystemExit('缺少 Python 官方运行时或 Windows wheels')
    with zipfile.ZipFile(python_zip) as archive:archive.extractall(runtime)
    packages=runtime/'Lib/site-packages';packages.mkdir(parents=True)
    artifacts=[{'name':python_zip.name,'sha256':hashlib.sha256(python_zip.read_bytes()).hexdigest(),
                'source':f'https://www.python.org/ftp/python/{PYTHON}/{python_zip.name}'}]
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                name=member.filename
                if name.startswith('/') or '..' in Path(name).parts:raise ValueError('wheel 路径无效')
                # Wheel .data payload must be installed in the appropriate directory.
                parts=name.split('/')
                if len(parts)>2 and parts[0].endswith('.data'):
                    if parts[1] not in ('purelib','platlib'):continue
                    name='/'.join(parts[2:])
                target=packages/name
                if member.is_dir():target.mkdir(parents=True,exist_ok=True)
                else:target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(member))
        artifacts.append({'name':wheel.name,'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'source':'https://pypi.org/'})
    (runtime/'python313._pth').write_text('python313.zip\n.\nLib\\site-packages\n..\\app\nimport site\n',encoding='utf-8')
    app=output/'app';app.mkdir()
    for name in ('app.py','launcher.py','chatlog_extract.py','chatlog_platform.py'):
        shutil.copyfile(ROOT/name,app/name)
    for name in ('web','wechatbrain'):
        shutil.copytree(ROOT/name,app/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copyfile(ROOT/'THIRD_PARTY_NOTICES.txt',output/'THIRD_PARTY_NOTICES.txt')
    shutil.copytree(ROOT/'third_party',output/'third_party')
    for name in ('win3','win4','ios_backup'):
        shutil.copytree(ROOT/'fixtures'/name,app/'fixtures'/name)
    cmd='@echo off\r\ncd /d "%~dp0"\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0app\\launcher.py"\r\n'
    (output/'启动京客导出助手.cmd').write_bytes(cmd.encode('utf-8'))
    (output/'演示模式.cmd').write_bytes(cmd.replace('launcher.py"','launcher.py" --demo').encode('utf-8'))
    (output/'浏览器模式.cmd').write_bytes(cmd.replace('launcher.py"','launcher.py" --browser').encode('utf-8'))
    guide=f'''京客导出助手 {VERSION} · Windows x64 便携版

1. 把整个压缩包解压到 Windows 上的文件夹。
2. 双击「启动京客导出助手.cmd」。运行环境和依赖已随包提供，无需另装 Python。
3. 助手自动发现本机微信账号，选择账号并点击「下一步」，软件会读取并进入聊天；不需要选择数据文件夹。
4. 在「设置 AI 与 Obsidian」填写模型服务和笔记库位置。
5. 点击「AI 提炼并保存全部」，逐人提炼重要事实、决定、约定、待办、偏好和事件。
6. 点击联系人查看提炼结果；重要笔记按生活／工作／其他分类，附原文依据。

「演示模式.cmd」只加载随包提供的合成记录，使用独立的临时资料库。
原生窗口不可用时会打开本机浏览器；也可直接使用「浏览器模式.cmd」。
原生窗口依赖 Microsoft Edge WebView2 Runtime。

本地资料库：%LOCALAPPDATA%\\WeChatBrain
导出位置：所选 Obsidian 库\\微信第二大脑\\人物
总索引：微信第二大脑\\重要记忆索引.md
默认只保存 AI 提炼的重要记忆，不批量抄录全部聊天原文。
没有新聊天时复用已有结果。模型未配置或提炼失败时，不会伪造 AI 笔记。
未知加密格式会列入报告，不会冒充读取成功。iPhone 加密备份使用原备份密码。
微信“备份与恢复”的专用包请先恢复到电脑版微信，再选择账号记录目录；iPhone Finder/iTunes 本地备份可直接选择完整目录。
程序中的未下载媒体消息目前保留类型占位，不能凭空恢复附件。

运行时来自 Python 官方，第三方包来自 PyPI，各包许可证保存在 runtime/Lib/site-packages。
组件版本和 SHA256 见 COMPONENTS.json。

聊天读取使用随包 app/chatlog_extract.py 和 app/chatlog_platform.py，
沿用本项目中 DeepSeek 编写的读取代码，由桌面导入模块直接调用。
Windows 3.x 连接与逐页校验使用 Chatlog 的 Python 适配实现，
来源和许可证见 THIRD_PARTY_NOTICES.txt 与 third_party/chatlog/LICENSE。
首次使用时请在保存记录的 Windows 电脑登录对应微信账号。助手会自动分析微信路径配置和常见位置，显示找到的账号。
软件会自动连接并校验，不需要在界面复制密钥。
标准加密 WAL 会在临时副本中校验并合并已提交页面，逐库结果见读取报告。
持续追加的新日志留给下一次同步；日志换代或基础库变化时重新获取完整副本。回滚日志、认证失败或反复变化会明确报错，保留已读记录，可正常退出微信后再读。
连接信息仅在本次软件运行的内存中保留，关闭软件后需要重新连接。

新版微信读取失败时：
1. 在账号读取进度里根据提示继续操作。
2. 可点击「等待手动重开」，再自行退出并重新打开微信、完成登录。
3. 也可确认已同步消息、保存编辑内容后，点击「重启微信并读取」。
4. 软件逐库校验后尝试读取，读取成功显示账号确认，点击「查看记录」。
5. 记录副本或日志校验失败时保持助手打开，正常退出微信后重试读取。
6. 仍失败时在处理结果点「复制读取诊断」，反馈系统、客户端版本与连接状态。
该连接模式由你主动启动，会临时连接本人微信进程，结束后自动解除连接。
内置 Frida 组件来自 PyPI，启动连接适配源于 MIT 许可的 wechat-decrypt。
0.6.1 新增独立实现的数据库入口连接，同时保留计算入口连接；所选加密数据库逐个校验。
连接检查会显示入口数量、候选数量和实际校验成功的数据库数量。
4.1.15.56 已在 Windows 虚拟机的已登录账号上通过真实读取：复用该账号本机已保存的连接材料，逐库完成校验。
这不代表没有任何连接材料时的首次连接、其他账号或所有微信版本已经完成兼容验收。

0.7.0 新增账号选择、已有缓存直接进入、账号导出目录、Windows 微信发现与打开、
登录/读取进度和账号确认页。软件不会模拟轻虾的服务账号、短信或会员登录。
微信扫码登录由官方电脑版微信窗口提供，读取成功与进程正在运行分开显示。
0.14.0 主流程改为自动发现账号→下一步→读取→自动进入聊天，无需手选或粘贴数据目录。
自动检查微信自身配置、系统文档位置、微信注册表及磁盘常见位置，包含微信自定义盘符；后台扫描不遍历附件内容。
多个账号由用户选择；找不到时打开官方微信并登录，助手会自动继续检测，也可点击重新检测。
手选目录、iPhone本地备份和其他来源放在「高级导入」，不占用默认账号流程。
在空资料库中实际运行：未预设数据路径，从微信配置自动发现1个本机账号，读入4个会话、12条消息，无读取错误。
这个小账号结果不代表另一台G盘电脑和全部历史媒体已经验收。
0.13.0 聊天页按参考布局重做：真实联系人头像和摘要、灰色消息区、绿色发出气泡。
选择微信总目录或账号目录只读取原生数据库，不把附件 JSON、文档和日期当成联系人；历史混入的文档会话保留但默认隐藏。
「全量刷新」重读当前会话；「仅读新消息」按消息游标追加；「自动同步」每3秒检查实际数据库与WAL。
基础库未变时校验并合并已提交WAL页面到私有副本，换代或基础库变化时重新读取；图片按当前消息证据准备。
这些功能覆盖本机已存在、已同步的记录，不能恢复微信未保存或尚未迁移的历史内容。
用户G盘历史大库位于另一台Windows，尚未在当前环境验收该库完整数量与新消息到达。
「语音转文字」尚未接入语音识别服务，当前不能转写；未接入原软件云端额度或商业后台。
0.12.0 新增按账号自动复用本地连接材料、联系人资料、真实账号名和空会话、4.x 本人发送方向修复、全部本地联系人导出。
本次实际读取4个会话、12条联系人资料、6条消息；Markdown和Excel联系人已实际导出。
0.11.0 新增先连接后复制、逐库材料收集、原导出器缓存自动发现、缓存联系人名称和同步完整性提示。
没有检测到微信进程时会明确显示当前环境，不会把此情况等同于文件不在本机。
0.10.0 新增每 500 条保存、未变化文件缓存复用、可取消扫描、详细进度和按需媒体预览。
聊天读取不再等待全目录媒体转换，读取中可查看已完成的聊天；完成文件可在下次继续读取时复用。
可选择原导出器的本地 msg_rows SQLite 缓存目录，读取缓存实际包含的数据；仅发现缓存不代表其中包含全部历史消息。
缓存副本保存在本机资料库的 source-cache，包含已解密内容；密码与读取访问材料仍不写入资料库。
0.9.0 新增本地媒体列表、图片解码、SILK/AMR 音频转换、视频转换和带附件导出。
DAT 支持旧版 XOR 和 V1/V2 分段格式；V2 会尝试从本地账号 kvcomm 材料自动推导并校验；尚无真实 V2 样本验收，不能保证全部版本。
WXGF 转换使用独立开源 FFmpeg；无法确认归属的媒体不自动绑定联系人。
0.8.0 新增记录与相邻 WAL 的变化检查、标准加密 WAL 完整提交合并、逐库读取报告。
日志校验失败会提示重试，不会一律要求重新登录。真实微信数据兼容性仍需验收。
'''
    (output/'使用说明.txt').write_text(guide,encoding='utf-8-sig')
    (output/'COMPONENTS.json').write_text(json.dumps(artifacts,ensure_ascii=False,indent=2),encoding='utf-8')
    guide += '\n导出中心：可导出当前/勾选/当前列表会话，支持 HTML/PDF/Excel/TXT/Markdown/CSV/JSON/JSONL。\n筛选条件：日期、关键词、消息类型、收发方向；可勾选具体消息。\n每次导出写入新的独立目录，附导出清单；不会覆盖之前的导出。\nPDF 使用本机 Edge/Chrome；媒体可在软件中查看，并随 HTML、Markdown 等导出。未读出的附件会列出具体原因。\n会话通讯录按选择范围导出；“导出账号全部联系人”还包含当前账号 contact.db 中没有消息的联系人，不代表云端全部好友或完整群成员。\n定时导出：应用打开期间运行，本地时间按一次/每天/每周触发。\n增量按本软件新导入消息计算，先重新读取聊天记录再执行计划。\n新版保留原 WeChatBrain 本地资料库与微信第二大脑 Obsidian 目录。\n功能对应情况与未完成项见源码 docs/京客导出助手-功能对应与证据.md。\n'
    (output/'使用说明.txt').write_text(guide,encoding='utf-8-sig')
    archive=shutil.make_archive(str(output),'zip',output.parent,output.name)
    # An explicit allowlist keeps real chat directories and local state out of source releases.
    source_zip=output.parent/f'JingkeExporter-{VERSION}-source.zip'
    names=('app.py','launcher.py','chatlog_extract.py','chatlog_platform.py','package_portable.py',
           'make_fixture.py','selftest.py','README.md','design-qa.md','requirements.txt','build_windows.ps1',
           '启动京客导出助手.cmd','.gitignore','THIRD_PARTY_NOTICES.txt')
    sources=[ROOT/name for name in names]
    for name in ('wechatbrain','web','tests','scripts','docs','fixtures','third_party'):
        sources.extend(p for p in (ROOT/name).rglob('*') if p.is_file()
                       and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo'))
    with zipfile.ZipFile(source_zip,'w',zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(set(sources)):
            bundle.write(path,f'JingkeExporter-{VERSION}-source/'+path.relative_to(ROOT).as_posix())
    for path in (Path(archive),source_zip):
        path.with_name(path.name+'.sha256').write_text(
            hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name+'\n',encoding='utf-8')
    print(json.dumps({'folder':str(output),'archive':archive,'source':str(source_zip),
                      'bytes':Path(archive).stat().st_size},ensure_ascii=False))


if __name__=='__main__':build()
