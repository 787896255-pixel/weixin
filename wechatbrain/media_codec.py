# SPDX-License-Identifier: Apache-2.0
"""Local media decoding; DAT/WXGF layout adapted from Apache-2.0 Chatlog.

See THIRD_PARTY_NOTICES.txt. No network requests or vendor binaries.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import time
import wave
import struct
import sys
from pathlib import Path


class MediaError(ValueError):
    pass


def image_format(data):
    if data.startswith(b'\xff\xd8\xff'): return 'jpg', 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'png', 'image/png'
    if data.startswith((b'GIF87a', b'GIF89a')): return 'gif', 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP': return 'webp', 'image/webp'
    if data.startswith(b'BM'): return 'bmp', 'image/bmp'
    if data.startswith((b'II*\x00', b'MM\x00*')): return 'tiff', 'image/tiff'
    return None


def _validated_image(data):
    kind = image_format(data)
    if not kind: raise MediaError('没有识别到可显示的图片')
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 80_000_000:
                raise MediaError('图片尺寸超过本次解码上限')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            frames=getattr(image,'n_frames',1)
            if frames>1000 or image.width*image.height*frames>500_000_000:
                raise MediaError('动图尺寸或帧数超过本次解码上限')
            for n in range(frames):image.seek(n);image.load()
    except ImportError:
        raise MediaError('图片解码组件缺失，请使用完整 Windows 包') from None
    except MediaError:
        raise
    except Exception:
        raise MediaError('图片内容不完整或校验失败') from None
    if kind[0] == 'tiff':
        # WebView2 and offline HTML do not consistently display TIFF.
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            converted = io.BytesIO()
            image.convert('RGBA').save(converted, format='PNG')
        return converted.getvalue(), 'png', 'image/png'
    return data, *kind


def decode_image(data, aes_keys=(), xor_keys=(), cancelled=lambda: False):
    if cancelled(): raise MediaError('已停止媒体处理')
    if len(data) > 64 * 1024 * 1024:
        raise MediaError('图片超过 64 MB 解码上限')
    if image_format(data): return _validated_image(data)
    if data.startswith(b'wxgf'):
        return decode_wxgf(data, cancelled)
    if data.startswith((b'\x07\x08V1\x08\x07', b'\x07\x08V2\x08\x07')):
        return decode_dat_v4(data,aes_keys,xor_keys,cancelled)
    # A candidate must match an entire known signature, then the image decoder.
    signatures = (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a', b'BM', b'wxgf')
    for signature in signatures:
        if len(data) < len(signature): continue
        key = data[0] ^ signature[0]
        if bytes(b ^ key for b in data[:len(signature)]) != signature: continue
        try:
            result=data.translate(bytes(n ^ key for n in range(256)))
            return decode_wxgf(result,cancelled) if result.startswith(b'wxgf') else _validated_image(result)
        except MediaError: continue
    # WebP's size field prevents a single contiguous signature match.
    if len(data) >= 12:
        key = data[0] ^ ord('R')
        head = bytes(b ^ key for b in data[:12])
        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
            return _validated_image(data.translate(bytes(n ^ key for n in range(256))))
    raise MediaError('尚未识别此媒体的图片格式')


def decode_dat_v4(data,aes_keys,xor_keys,cancelled=lambda: False):
    from Crypto.Cipher import AES
    if len(data)<15:raise MediaError('新版图片头不完整')
    encrypted,tail=struct.unpack('<II',data[6:14]);body=data[15:]
    count=encrypted//16*16+16
    if count+tail>len(body):raise MediaError('新版图片数据长度不完整')
    candidates=[b'cfcd208495d565ef'] if data[3:4]==b'1' else list(aes_keys)
    if not candidates:raise MediaError('新版图片尚未取得该账号的有效图片连接信息')
    tails=list(dict.fromkeys([*xor_keys,0x37]))
    for signature in (b'\xff\xd9',b'IEND\xaeB\x60\x82'):
        if tail>=len(signature):
            candidate=data[-1]^signature[-1]
            if bytes(v^candidate for v in data[-len(signature):])==signature:tails.insert(0,candidate)
    if not tail:tails=[0]
    for key in candidates:
        if cancelled(): raise MediaError('已停止媒体处理')
        if len(key)!=16:continue
        plain=AES.new(key,AES.MODE_ECB).decrypt(body[:count])
        padding=count-encrypted
        if not 1<=padding<=16 or plain[-padding:]!=bytes([padding])*padding:continue
        for xor in tails:
            result=plain[:encrypted]+body[count:len(body)-tail if tail else len(body)]
            if tail:result+=body[-tail:].translate(bytes(n^xor for n in range(256)))
            try:return decode_wxgf(result,cancelled) if result.startswith(b'wxgf') else _validated_image(result)
            except MediaError:continue
    raise MediaError('新版图片连接信息或内容未通过校验')


def decode_wxgf(data,cancelled=lambda: False):
    if len(data)<15 or not data.startswith(b'wxgf'):raise MediaError('WXGF 图片头无效')
    # Some containers carry a standard image alongside HEVC payloads. Validate
    # complete content instead of accepting a marker inside arbitrary bytes.
    for signature, ending in ((b'\xff\xd8\xff',b'\xff\xd9'),
                              (b'\x89PNG\r\n\x1a\n',b'IEND\xaeB\x60\x82')):
        start = 4
        for _ in range(8):
            if cancelled():raise MediaError('已停止媒体处理')
            pos=data.find(signature,start)
            if pos<0:break
            end=data.find(ending,pos+len(signature))
            if end>=0:
                try:return _validated_image(data[pos:end+len(ending)])
                except MediaError:pass
            start=pos+len(signature)
    offset=data[4]
    if offset>=len(data):raise MediaError('WXGF 图片头长度无效')
    parts=[]
    for marker in (b'\x00\x00\x00\x01',b'\x00\x00\x01'):
        start=offset
        while start<len(data) and len(parts)<10000:
            if cancelled():raise MediaError('已停止媒体处理')
            pos=data.find(marker,start)
            if pos<0:break
            length=int.from_bytes(data[pos-4:pos],'big') if pos>=4 else 0
            if length>0 and pos+length<=len(data):parts.append(data[pos:pos+length]);start=pos+length
            else:start=pos+1
        if parts:break
    if not parts:raise MediaError('WXGF 中没有识别到完整 HEVC 图像段')
    animated=len(parts)>1 and max(map(len,parts))/len(data)<.6
    with tempfile.TemporaryDirectory(prefix='jingke-wxgf-') as work:
        root=Path(work);source=root/'color.h265'
        if animated:
            source.write_bytes(b''.join(parts[1::2]));mask=root/'mask.h265';mask.write_bytes(b''.join(parts[::2]))
            output=root/'image.gif'
            _transcode(source,output,['-protocol_whitelist','file,pipe','-i',str(mask),'-filter_complex',
                '[0:v][1:v]alphamerge,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse','-loop','0'],cancelled,timeout=45)
        else:
            source.write_bytes(max(parts,key=len));output=root/'image.jpg'
            _transcode(source,output,['-frames:v','1','-q:v','3'],cancelled,timeout=30)
        return _validated_image(output.read_bytes())


def ffmpeg_path():
    # Use our separately shipped, open-source codec or an installed FFmpeg.
    try:
        import imageio_ffmpeg
        root = Path(imageio_ffmpeg.__file__).parent / 'binaries'
        candidates = sorted(root.glob('ffmpeg-*'))
        for p in candidates:
            if p.is_file() and (p.suffix == '.exe' if os.name == 'nt' else os.access(p, os.X_OK)):
                return str(p)
    except ImportError:
        pass
    path = shutil.which('ffmpeg')
    if path: return path
    raise MediaError('音视频解码组件缺失，请使用完整 Windows 包')


def _run_codec(command, output, cancelled=lambda: False, timeout=180):
    kwargs = {'creationflags': 0x08000000} if os.name == 'nt' else {}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,cwd=Path(__file__).resolve().parent.parent, **kwargs)
    started = time.monotonic()
    try:
        while process.poll() is None:
            if cancelled(): raise MediaError('已停止媒体处理')
            if time.monotonic() - started > timeout: raise MediaError('音视频转换超时')
            time.sleep(.1)
        if process.returncode or not output.is_file() or not output.stat().st_size:
            raise MediaError('音视频解码失败，原文件已保留')
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
        output.unlink(missing_ok=True)
        raise


def _transcode(source, output, args, cancelled=lambda: False, timeout=180):
    command = [ffmpeg_path(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
               '-protocol_whitelist', 'file,pipe', '-i', str(source), *args, str(output)]
    _run_codec(command,output,cancelled,timeout)


def voice_payload(data):
    """Locate a voice header inside the first 64 bytes of a local voice blob."""
    pos=data.find(b'#!SILK_V3',0,64)
    if pos>=0:
        start=pos-1 if pos and data[pos-1]==2 else pos
        return data[start:], '.silk'
    pos=data.find(b'#!AMR',0,64)
    if pos>=0:return data[pos:], '.amr'
    return data, ''


def convert_audio(source, output, cancelled=lambda: False):
    with source.open('rb') as stream: head = stream.read(64)
    if b'#!SILK_V3' in head:
        if source.stat().st_size>2*1024*1024:raise MediaError('SILK 语音超过单条解码上限')
        with tempfile.TemporaryDirectory(prefix='jingke-silk-') as work:
            pcm = Path(work) / 'voice.pcm'
            try:
                normalized = Path(work)/'source.silk'
                normalized.write_bytes(voice_payload(source.read_bytes())[0])
                _run_codec([sys.executable,'-m','wechatbrain.media_codec','silk-pcm',str(normalized),str(pcm)],
                           pcm,cancelled,60)
                if cancelled(): raise MediaError('已停止媒体处理')
                if not pcm.stat().st_size or pcm.stat().st_size % 2:
                    raise MediaError('语音未产生有效音频采样')
                with wave.open(str(output), 'wb') as out, pcm.open('rb') as inp:
                    out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
                    while True:
                        if cancelled(): raise MediaError('已停止媒体处理')
                        chunk = inp.read(1024 * 1024)
                        if not chunk: break
                        out.writeframesraw(chunk)
            except MediaError: raise
            except Exception: raise MediaError('SILK 语音解码失败') from None
    else:
        with tempfile.TemporaryDirectory(prefix='jingke-audio-') as work:
            if b'#!AMR' in head and not head.startswith(b'#!AMR'):
                normalized=Path(work)/'source.amr'
                normalized.write_bytes(voice_payload(source.read_bytes())[0])
                source=normalized
            _transcode(source, output, ['-vn', '-ac', '1', '-ar', '24000', '-c:a', 'pcm_s16le'], cancelled, 60)
    return 'wav', 'audio/wav'


def convert_video(source, output, cancelled=lambda: False):
    _transcode(source, output, ['-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264',
                               '-preset', 'fast', '-crf', '23', '-pix_fmt', 'yuv420p',
                               '-c:a', 'aac', '-movflags', '+faststart'], cancelled)
    return 'mp4', 'video/mp4'


if __name__=='__main__':
    if len(sys.argv)!=4 or sys.argv[1]!='silk-pcm':raise SystemExit(2)
    try:
        import pysilk
        Path(sys.argv[3]).write_bytes(pysilk.decode(Path(sys.argv[2]).read_bytes(),sample_rate=24000))
    except Exception:raise SystemExit(1)
