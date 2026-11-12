import json
import os
from pathlib import Path
from typing import Union

from videotrans import translator
from videotrans.configure.config import tr, ROOT_DIR, settings, logger
from videotrans.configure import constants
from videotrans.configure.excepts import VideoTransError
from videotrans.util.help_srt import get_subtitle_from_srt, simple_wrap, set_ass_font, ms_to_time_string
from videotrans.util._srt_ass import auto_layout
from videotrans.task.taskcfg import SrtItem


class SubtitleMixin:

    def _process_subtitles(self) -> Union[tuple[str, str], None]:
        _subtitles = [
            tr('nosubtitle'),
            tr('embedsubtitle'),
            tr('softsubtitle'),
            tr('embedsubtitle2'),
            tr('softsubtitle2')
        ]
        logger.debug(f"\n准备要嵌入的字幕:{self.cfg.subtitle_type=}\n{self.cfg.target_sub=}")
        if not Path(self.cfg.target_sub).exists() :
            raise VideoTransError(tr("No valid subtitle file exists")+self.cfg.target_sub)

        if not Path(self.cfg.source_sub).exists() or (self.cfg.source_language_code == self.cfg.target_language_code):
            if self.cfg.subtitle_type == 3:
                self.cfg.subtitle_type = 1
            elif self.cfg.subtitle_type == 4:
                self.cfg.subtitle_type = 2

        process_end_subtitle = self.cfg.cache_folder + f'/end.srt'
        maxlen = int(
            settings.get('cjk_len', 15) if self.cfg.target_language_code[:2] in constants.CJK_LANG else
            settings.get('other_len', 60))
        target_sub_list = get_subtitle_from_srt(self.cfg.target_sub)

        # Without a custom ass.json, size lines to the video and keep each cue to 2 lines
        _size = self._video_size()
        _auto = self.cfg.subtitle_type in [1, 3] and _size and not Path(f'{ROOT_DIR}/videotrans/ass.json').exists()
        if _auto:
            _lay = auto_layout(*_size)
            _cpl = lambda code: _lay['cjk_chars_per_line'] if code[:2] in constants.CJK_LANG else _lay['chars_per_line']
            maxlen = min(maxlen, _cpl(self.cfg.target_language_code))
            if self.cfg.subtitle_type == 1:
                target_sub_list = self._split_long_cues(target_sub_list, maxlen * 2)
        # In auto mode libass's smart wrapping balances the two lines better than a hard wrap
        _wrap = (lambda text, _maxlen, _code: text) if _auto else simple_wrap

        srt_string = ""
        _join_flag = ''
        if self.cfg.subtitle_type in [3, 4]:
            source_sub_list = get_subtitle_from_srt(self.cfg.source_sub)
            source_length = len(source_sub_list)
            source_maxlen = int(
                settings.get('cjk_len', 15) if self.cfg.source_language_code.split('-')[0] in constants.CJK_LANG   else
                settings.get('other_len', 60))
            if _auto:
                source_maxlen = min(source_maxlen, _cpl(self.cfg.source_language_code))

            _join_flag = self._get_join_flag()

            for i, it in enumerate(target_sub_list):
                _text = _wrap(it['text'].strip(), maxlen, self.cfg.target_language_code)
                srt_string += f"{it['line']}\n{it['time']}\n"
                if source_length > 0 and i < source_length:
                    _text_source = _wrap(source_sub_list[i]['text'], source_maxlen,
                                                     self.cfg.source_language_code)
                    _text = f'{_text_source}\n{_join_flag}{_text}' if self.cfg.output_srt == 1 else f'{_text}\n{_join_flag}{_text_source}'
                srt_string += f"{_text}\n\n"
            srt_string = srt_string.strip()
            process_end_subtitle = f"{self.cfg.cache_folder}/shuang.srt"
            Path(process_end_subtitle).write_text(srt_string, encoding='utf-8')
            Path(self.cfg.target_dir + "/shuang.srt").write_text(
                srt_string.replace('###', '') if _join_flag == '###' else srt_string, encoding='utf-8')
        else:
            for i, it in enumerate(target_sub_list):
                tmp = _wrap(it['text'].strip(), maxlen, self.cfg.target_language_code)
                srt_string += f"{it['line']}\n{it['time']}\n{tmp.strip()}\n\n"
            with Path(process_end_subtitle).open('w', encoding='utf-8') as f:
                f.write(srt_string)

        subtitle_langcode = translator.get_subtitle_code(show_target=self.cfg.target_language)
        _msg=f'最终确定字幕嵌入类型:{_subtitles[self.cfg.subtitle_type]} ,目标字幕语言:{subtitle_langcode}, '
        if self.cfg.subtitle_type in [2, 4]:
            _msg+=f'字幕文件:{process_end_subtitle}\n'
            logger.debug(_msg)
            return os.path.basename(process_end_subtitle), subtitle_langcode

        process_end_subtitle_ass = set_ass_font(process_end_subtitle, video_size=_size)
        _msg+=f'字幕文件:{process_end_subtitle_ass}\n'
        logger.debug(_msg)
        basename = os.path.basename(process_end_subtitle_ass)
        return basename, subtitle_langcode

    def _video_size(self):
        info = getattr(self, 'video_info', None) or {}
        w, h = int(info.get('width') or 0), int(info.get('height') or 0)
        return (w, h) if w and h else None

    @staticmethod
    def _split_long_cues(sub_list, max_chars):
        """Split cues longer than max_chars at word/punctuation boundaries, sharing the
        cue's time span in proportion to text length."""
        import re
        out = []
        for it in sub_list:
            text = re.sub(r'\s+', ' ', it['text']).strip()
            if len(text) <= max_chars:
                out.append(it)
                continue
            words = text.split(' ') if ' ' in text else list(text)
            sep = ' ' if ' ' in text else ''
            chunks, cur = [], ''
            for w in words:
                cand = f'{cur}{sep}{w}' if cur else w
                if len(cand) > max_chars and cur:
                    chunks.append(cur)
                    cur = w
                else:
                    cur = cand
                # Prefer breaking after sentence/clause punctuation past the halfway point
                if len(cur) >= max_chars * 0.5 and re.search(r'[.!?,;:。！？，、]$', cur):
                    chunks.append(cur)
                    cur = ''
            if cur:
                chunks.append(cur)
            start, end = it['start_time'], it['end_time']
            total = sum(len(c) for c in chunks) or 1
            t = start
            for i, c in enumerate(chunks):
                t_end = end if i == len(chunks) - 1 else round(t + (end - start) * len(c) / total)
                startraw, endraw = ms_to_time_string(ms=t), ms_to_time_string(ms=t_end)
                out.append(SrtItem(text=c, start_time=t, end_time=t_end, startraw=startraw, endraw=endraw,
                                   time=f"{startraw} --> {endraw}", spk=it.get('spk', '')))
                t = t_end
        for i, it in enumerate(out, 1):
            it['line'] = i
        return out

    def _get_join_flag(self):
        _join_flag = ""
        if self.cfg.subtitle_type != 3 or not Path(f'{ROOT_DIR}/videotrans/ass.json').exists():
            return _join_flag
        try:
            assjson = json.loads(Path(f'{ROOT_DIR}/videotrans/ass.json').read_text(encoding='utf-8'))
        except Exception:
            logger.warning(f'未自定义样式 ass.json ，忽略')
            return _join_flag
        else:
            for k, v in assjson.items():
                if k.startswith('Bottom_') and v != assjson.get(k[7:]):
                    _join_flag = '###'
                    break
        return _join_flag
