# -*- coding: utf-8 -*-
import os, json, re
from pathlib import Path

from videotrans.configure.config import ROOT_DIR, logger
from videotrans.util._srt_parse import get_subtitle_from_srt


def auto_layout(width: int, height: int) -> dict:
    """Subtitle geometry scaled to the video, used when no custom ass.json is saved.

    ffmpeg's SRT->ASS default (PlayRes 384x288, 16pt, margin 10) is far too large on
    vertical video and sits under the TikTok/Reels/Shorts caption and button overlay.
    """
    short = min(width, height)
    vertical = height > width
    fontsize = round(short * 0.066)
    margin_lr = round(width * 0.07)
    return {
        'fontsize': fontsize,
        'outline': max(2, round(fontsize * 0.1)),
        'shadow': max(1, round(fontsize * 0.04)),
        'margin_lr': margin_lr,
        # Lift above the bottom overlay on short-form platforms
        'margin_v': round(height * (0.2 if vertical else 0.07)),
        # Approximate characters per line at this size (bold sans, Latin ~0.55em)
        'chars_per_line': max(12, int((width - 2 * margin_lr) / (fontsize * 0.55))),
        'cjk_chars_per_line': max(6, int((width - 2 * margin_lr) / fontsize)),
    }


def _apply_auto_style(ass_file_path: str, width: int, height: int) -> None:
    lay = auto_layout(width, height)
    fs, out, sh = lay['fontsize'], lay['outline'], lay['shadow']
    m, mv = lay['margin_lr'], lay['margin_v']
    styles = (
        f"Style: Default,Noto Sans,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{out},{sh},2,{m},{m},{mv},1\n"
        f"Style: Bottom,Noto Sans,{round(fs * 0.8)},&H0000F0FF,&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{out},{sh},2,{m},{m},{mv},1\n"
    )
    content = Path(ass_file_path).read_text(encoding='utf-8-sig')
    content = re.sub(r'^PlayResX:.*$', f'PlayResX: {width}', content, flags=re.M)
    content = re.sub(r'^PlayResY:.*$', f'PlayResY: {height}', content, flags=re.M)
    content = re.sub(r'^(Format: Name,[^\n]*\n)(?:Style:[^\n]*\n)+', lambda mt: mt.group(1) + styles, content,
                     count=1, flags=re.M)
    Path(ass_file_path).write_text(content, encoding='utf-8')


def set_ass_font(srtfile: str, video_size: tuple = None) -> str:
    from . import help_ffmpeg
    """
    Convert SRT to ASS with custom styles:
    - Main style (Default) for primary text
    - Bottom style for secondary text after '###' marker
    - Remove '###' markers
    """
    if not os.path.exists(srtfile) or os.path.getsize(srtfile) == 0:
        return os.path.basename(srtfile)

    srt_str = ""
    for it in get_subtitle_from_srt(srtfile, is_file=True):
        text = re.sub(r'\n|\\n', r'\\N', it['text'].strip())
        if text:
            _time = f'{it["startraw"]} --> {it["endraw"]}'
            _time = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{2})\d?', r'\1,\g<2>0', _time)
            srt_str += f'{it["line"]}\n{_time}\n{text}\n\n'
    edit_srt = srtfile[:-4] + '-edit.srt'
    with open(edit_srt, 'w', encoding='utf-8') as f:
        f.write(srt_str.strip())
    ass_file_path = f'{srtfile[:-3]}ass'
    help_ffmpeg.runffmpeg(['-y', '-i', edit_srt, ass_file_path])

    JSON_FILE = f'{ROOT_DIR}/videotrans/ass.json'
    if not os.path.exists(JSON_FILE):
        if video_size and video_size[0] and video_size[1]:
            try:
                _apply_auto_style(ass_file_path, int(video_size[0]), int(video_size[1]))
                logger.debug(f"[set_ass_font] no ass.json; applied auto style for {video_size}")
            except Exception as e:
                logger.exception(f"[set_ass_font] auto style failed: {e}", exc_info=True)
        return ass_file_path

    try:
        with open(JSON_FILE, 'r', encoding='utf-8-sig') as f:
            style = json.load(f)
    except Exception as e:
        logger.exception(f"[set_ass_font] 错误：无法读取或解析 JSON 文件 {JSON_FILE}: {e}", exc_info=True)
        return ass_file_path

    default_style = (
        f"Style: {style.get('Name', 'Default')},"
        f"{style.get('Fontname', 'Arial')},"
        f"{style.get('Fontsize', 16)},"
        f"{style.get('PrimaryColour', '&H00FFFFFF&')},"
        f"{style.get('SecondaryColour', '&H00FFFFFF&')},"
        f"{style.get('OutlineColour', '&H00000000&')},"
        f"{style.get('BackColour', '&H00000000&')},"
        f"{style.get('Bold', 0)},"
        f"{style.get('Italic', 0)},"
        f"{style.get('Underline', 0)},"
        f"{style.get('StrikeOut', 0)},"
        f"{style.get('ScaleX', 100)},"
        f"{style.get('ScaleY', 100)},"
        f"{style.get('Spacing', 0)},"
        f"{style.get('Angle', 0)},"
        f"{style.get('BorderStyle', 1)},"
        f"{style.get('Outline', 1)},"
        f"{style.get('Shadow', 0)},"
        f"{style.get('Alignment', 2)},"
        f"{style.get('MarginL', 10)},"
        f"{style.get('MarginR', 10)},"
        f"{style.get('MarginV', 10)},"
        f"{style.get('Encoding', 1)}\n"
    )

    # 双语时底部语言的字体，ass.json 中没有或为空时使用主字体
    bottom_fontname = style.get('Bottom_Fontname') or style.get('Fontname', 'Arial')
    bottom_fontsize = style.get('Bottom_Fontsize', 14)
    bottom_color = style.get('Bottom_PrimaryColour', '&H0000FFFF&')
    bottom_bold = style.get('Bottom_Bold', 0)
    bottom_italic = style.get('Bottom_Italic', 0)
    bottom_secondarycolour = style.get('Bottom_SecondaryColour', '&H00FFFFFF&')
    bottom_outlinecolour = style.get('Bottom_OutlineColour', '&H00000000&')
    bottom_backcolour = style.get('Bottom_BackColour', '&H00000000&')

    bottom_style = (
        f"Style: Bottom,"
        f"{bottom_fontname},"
        f"{bottom_fontsize},"
        f"{bottom_color},"
        f"{bottom_secondarycolour},"
        f"{bottom_outlinecolour},"
        f"{bottom_backcolour},"
        f"{bottom_bold},"
        f"{bottom_italic},"
        f"{style.get('Underline', 0)},"
        f"{style.get('StrikeOut', 0)},"
        f"{style.get('ScaleX', 100)},"
        f"{style.get('ScaleY', 100)},"
        f"{style.get('Spacing', 0)},"
        f"{style.get('Angle', 0)},"
        f"{style.get('BorderStyle', 1)},"
        f"{style.get('Outline', 1)},"
        f"{style.get('Shadow', 0)},"
        f"{style.get('Alignment', 2)},"
        f"{style.get('MarginL', 10)},"
        f"{style.get('MarginR', 10)},"
        f"{style.get('MarginV', 10)},"
        f"{style.get('Encoding', 1)}\n"
    )

    try:
        with open(ass_file_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()
    except Exception as e:
        logger.exception(f"[set_ass_font] 错误：无法读取 ASS 文件: {e}", exc_info=True)
        return ass_file_path

    pattern = r'(^\[V4\+ Styles\]\s*\r?\n' \
              r'Format:[^\r\n]*\r?\n' \
              r'(?:Style:[^\r\n]*\r?\n)*)' \
              r'(?=\[|$)'

    def replacer(match):
        format_line = None
        for line in match.group(0).splitlines():
            if line.strip().startswith("Format:"):
                format_line = line.strip() + "\n"
                break
        if not format_line:
            format_line = "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        return f"[V4+ Styles]\n{format_line}{default_style}{bottom_style}"

    try:
        new_content, _ = re.subn(pattern, replacer, content, flags=re.MULTILINE)
    except Exception as e:
        logger.exception(f"[set_ass_font] 错误：正则替换样式失败: {e}", exc_info=True)
        return ass_file_path

    lines = new_content.splitlines(keepends=True)
    processed_lines = []
    inside_events = False
    dialogue_pattern = re.compile(r'^(Dialogue:.*?,.*?,.*?,.*?,.*?,.*?,.*?,.*?,.*?,)(.*)$')

    for line in lines:
        if line.strip().startswith('[Events]'):
            inside_events = True
        elif line.strip().startswith('[') and inside_events:
            inside_events = False

        if inside_events and line.startswith('Dialogue:'):
            match = dialogue_pattern.match(line.rstrip('\r\n'))
            if match:
                prefix = match.group(1)
                text = match.group(2)
                if '###' in text:
                    parts = text.split('###', 1)
                    left = parts[0]
                    right = parts[1] if len(parts) > 1 else ''
                    new_text = ''
                    if left:
                        new_text += left
                    if right:
                        new_text += f'{{\\rBottom}}{right}{{\\r}}'
                    line = f'{prefix}{new_text}\n'
        processed_lines.append(line)

    try:
        with open(ass_file_path, 'w', encoding='utf-8', newline='') as f:
            f.writelines(processed_lines)
    except Exception as e:
        logger.exception(f"[set_ass_font] 错误：无法写入 ASS 文件: {e}", exc_info=True)

    return ass_file_path
