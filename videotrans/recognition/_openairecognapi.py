# zh_recogn 识别
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List,  Union

import httpx
from openai import OpenAI,APIConnectionError,APIError
from videotrans.configure.config import params,  logger,tr
from videotrans.configure import config
from videotrans.configure.excepts import SpeechToTextError, StopTask
from videotrans.recognition._base import BaseRecogn
from videotrans.task.taskcfg import SrtItem
from videotrans.util._ffmpeg_runner import runffmpeg
from videotrans.util._srt_parse import ms_to_time_string


@dataclass(repr=False)
class OpenaiAPIRecogn(BaseRecogn):

    def __post_init__(self):
        super().__post_init__()
        self.api_url = params.get('openairecognapi_api', '')


    def _exec(self) -> Union[List[SrtItem], None]:
        if self._exit(): return
        # 如果是 gpt-4o-transcribe-diarize 说话人识别默认
        try:
            if self.model_name.lower() == 'gpt-4o-transcribe-diarize':
                return self._diarize()
            
            # 如果是第三方
            if not re.search(r'api\.openai\.com/v1', self.api_url):
                return self._thrid_api()

            mp3_tmp = config.TEMP_DIR + f'/recogn{time.time()}.mp3'
            runffmpeg([
                "-y",
                "-i",
                Path(self.audio_file).as_posix(),
                "-ac",
                "1",
                "-ar",
                "16000",
                mp3_tmp
            ])

            self.audio_file = mp3_tmp
            if not Path(self.audio_file).is_file():
                raise SpeechToTextError(f'No {self.audio_file}')
            # 发送请求
            raws = []
            client = OpenAI(api_key=params.get('openairecognapi_key', ''), base_url=self.api_url,
                            http_client=httpx.Client(proxy=self.proxy_str))
            with open(self.audio_file, 'rb') as file:
                transcript = client.audio.transcriptions.create(
                    file=(os.path.basename(self.audio_file), file.read()),
                    model=self.model_name,
                    prompt=params.get('openairecognapi_prompt', ''),
                    language=self.detect_language.split('-')[0],
                    response_format="verbose_json",
                    #chunking_strategy='auto',
                    timestamp_granularities=["segment"]
                )
                if not hasattr(transcript, 'segments'):
                    return self._thrid_api()
                for i, it in enumerate(transcript.segments):
                    raws.append(SrtItem(
                        line=len(raws) + 1,
                        start_time=it.start * 1000,
                        end_time=it.end * 1000,
                        text=it.text,
                        time=ms_to_time_string(ms=it.start * 1000) + ' --> ' + ms_to_time_string(
                            ms=it.end * 1000),
                    ))
        except APIConnectionError as e:
            raise StopTask(f' {tr("Unable to connect to API",self.api_url)}\n{e}') from e
        except APIError as e: 
            if re.search(r"insufficient.*?balance",e.message,flags=re.I):
                raise StopTask(tr('The server returned an error message: Insufficient balance',tr("OpenAI Speech to Text"),self.api_url))
            raise
        return raws

    def _thrid_api(self)->Union[List[SrtItem], None]:
        # Whole-file request with word timestamps (Groq supports verbose_json + word granularity):
        # one call instead of one per VAD chunk, and cue timing follows the actual speech.
        if 'groq.com' in self.api_url:
            try:
                cues = self._whole_file_words()
                if cues:
                    return cues
            except StopTask:
                raise
            except Exception as e:
                logger.warning(f'[openairecognapi] whole-file word timestamps failed, falling back to VAD chunks: {e}')
        # 发送请求
        raws = self.cut_audio()
        client = OpenAI(
            api_key=params.get('openairecognapi_key', ''),
            base_url=self.api_url,
            http_client=httpx.Client(proxy=self.proxy_str or None)
        )
        for i, it in enumerate(raws):
            with open(it['filename'], 'rb') as file:
                transcript = client.audio.transcriptions.create(
                    file=(os.path.basename(it['filename']), file.read()),
                    model=self.model_name,
                    prompt=params.get('openairecognapi_prompt', ''),
                    # timeout=7200,
                    language=self.detect_language.split('-')[0],
                    response_format="json"
                )
                if not hasattr(transcript, 'text') or not transcript.text or not transcript.text.strip():
                    continue
                raws[i]['text'] = transcript.text
            if self.asr_wait>0:
                time.sleep(self.asr_wait)
        return raws

    # Groq's upload cap is ~25MB; 16kHz mono at 48kbps is ~0.36MB/min, so this covers ~1h
    MAX_UPLOAD_BYTES = 24 * 1024 * 1024

    def _whole_file_words(self) -> Union[List[SrtItem], None]:
        mp3_tmp = config.TEMP_DIR + f'/recogn_whole{time.time()}.mp3'
        runffmpeg(["-y", "-i", Path(self.audio_file).as_posix(), "-ac", "1", "-ar", "16000", "-b:a", "48k", mp3_tmp])
        if not Path(mp3_tmp).is_file() or Path(mp3_tmp).stat().st_size > self.MAX_UPLOAD_BYTES:
            return None
        client = OpenAI(
            api_key=params.get('openairecognapi_key', ''),
            base_url=self.api_url,
            http_client=httpx.Client(proxy=self.proxy_str or None)
        )
        lang = self.detect_language.split('-')[0] if self.detect_language and self.detect_language != 'auto' else None
        kwargs = dict(
            file=(os.path.basename(mp3_tmp), Path(mp3_tmp).read_bytes()),
            model=self.model_name,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
        if lang:
            kwargs['language'] = lang
        if params.get('openairecognapi_prompt', ''):
            kwargs['prompt'] = params.get('openairecognapi_prompt', '')
        transcript = client.audio.transcriptions.create(**kwargs)
        data = transcript if isinstance(transcript, dict) else transcript.model_dump()
        words = [w for w in (data.get('words') or []) if str(w.get('word', '')).strip()]
        if not words:
            return None
        cjk = (lang or data.get('language', '') or '')[:2] in ('zh', 'ja', 'ko', 'yu') or \
              str(data.get('language', '')).lower() in ('chinese', 'japanese', 'korean', 'cantonese')
        sentences = self._group_words(words, joiner='' if cjk else ' ')
        logger.debug(f'[openairecognapi] whole-file: {len(words)} words -> {len(sentences)} sentences')
        out = []
        for i, (start, end, text) in enumerate(sentences, 1):
            start_ms, end_ms = int(start * 1000), int(end * 1000)
            startraw, endraw = ms_to_time_string(ms=start_ms), ms_to_time_string(ms=end_ms)
            out.append(SrtItem(line=i, text=text, start_time=start_ms, end_time=end_ms,
                               startraw=startraw, endraw=endraw, time=f'{startraw} --> {endraw}'))
        return out

    @staticmethod
    def _group_words(words, joiner=' ', max_gap=0.7, max_duration=8.0, max_chars=110):
        """Group timed words into sentence-sized cues: these are the translation units.

        A cue ends at sentence punctuation, at a pause, or when it grows too long (preferring
        a comma). Display splitting into two-line chunks happens later, after translation.
        """
        sentences, cur = [], []

        def flush():
            if cur:
                text = joiner.join(str(w['word']).strip() for w in cur)
                text = re.sub(r'\s+([,.!?;:%])', r'\1', text).strip()
                sentences.append((float(cur[0]['start']), float(cur[-1]['end']), text))
                cur.clear()

        for w in words:
            token = str(w['word']).strip()
            if cur:
                gap = float(w['start']) - float(cur[-1]['end'])
                length = sum(len(str(x['word']).strip()) + len(joiner) for x in cur)
                too_long = float(w['end']) - float(cur[0]['start']) > max_duration or length + len(token) > max_chars
                if gap >= max_gap or too_long:
                    flush()
            cur.append(w)
            if re.search(r'[.!?。！？…]["\'”’)]?$', token):
                flush()
            elif re.search(r'[,;:，；、]$', token):
                length = sum(len(str(x['word']).strip()) + len(joiner) for x in cur)
                if length > max_chars * 0.6 or float(cur[-1]['end']) - float(cur[0]['start']) > max_duration * 0.6:
                    flush()
        flush()
        return sentences

    def _diarize(self)->Union[List[SrtItem], None]:
        client = OpenAI(
            api_key=params.get('openairecognapi_key', ''),
            base_url=self.api_url
        )
        raws = []
        speaker_list = []
        speaker_name = []
        with open(self.audio_file, 'rb') as file:
            transcript = client.audio.transcriptions.create(
                file=(os.path.basename(self.audio_file), file.read()),
                model=self.model_name,
                language=self.detect_language.split('-')[0],
                chunking_strategy="auto",
                response_format="diarized_json"
            )

            if not hasattr(transcript, 'segments') or not transcript.segments:
                raise StopTask('No support gpt-4o-transcribe-diarize')
            for it in transcript.segments:
                raws.append(SrtItem(
                    line=len(raws) + 1,
                    start_time=it.start * 1000,
                    end_time=it.end * 1000,
                    text=it.text,
                    time=ms_to_time_string(ms=it.start * 1000) + ' --> ' + ms_to_time_string(
                        ms=it.end * 1000),
                ))
                if self.max_speakers>-1:
                    sp = getattr(it,"speaker",'-')
                    speaker_list.append(sp)
                    if sp not in speaker_name:
                        speaker_name.append(sp)

        if speaker_name:
            try:
                #默认未识别出后的回退说话人
                next_spk=f'spk{len(speaker_name)}'
                for i,it in enumerate(speaker_list):
                    if it=='-':
                        speaker_list[i]=next_spk
                    else:
                        speaker_list[i]=f'spk{speaker_name.index(it)}'
                if speaker_list:
                    Path(f'{self.cache_folder}/speaker.json').write_text(json.dumps(speaker_list), encoding='utf-8')
            except Exception as e:
                logger.exception(f'说话人重排序出错，忽略{e}',exc_info=True)
        return raws