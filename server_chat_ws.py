from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
import asyncio, base64, math, threading, queue, contextlib, time, os
from google.cloud import speech_v1 as speech
from google.cloud import texttospeech
from openai import OpenAI

# ===== Config =====
SAMPLE_RATE   = 16000
FRAME_BYTES   = 640                 # 20ms (16k * 2B * 0.02s)
SILENCE_MS    = 600
RMS_THRESH    = 500                 # ノイズが強いなら 800〜1200
FINAL_WAIT_MS = 500                 # フォールバック用（通常はFINAL即応答）
LANG          = "en-US"             # 英語STT/TTS
SEGMENT_SILENCE_MS = 2000           # 2秒無音でSTTストリーム再生成
STREAM_MAX_SEC      = 55            # 55秒ごとにロールオーバー
DEFAULT_TTS_VOICE   = os.getenv("TTS_VOICE", "en-US-Neural2-F")  # 例: en-US-Studio-O, en-US-Wavenet-D

app = FastAPI()
speech_client = speech.SpeechClient()
tts_client    = texttospeech.TextToSpeechClient()
oa            = OpenAI()  # OPENAI_API_KEY を環境変数に

# ===== Utils =====
def rms_int16(pcm: bytes) -> float:
    import array
    a = array.array('h'); a.frombytes(pcm)
    if not a: return 0.0
    s = sum(x*x for x in a)
    return math.sqrt(s/len(a))

def synth_tts_16k_linear16(text: str) -> bytes:
    audio_cfg = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        speaking_rate=1.05,                 # 少し速め
        pitch=-2.0,                         # 少し低め
        volume_gain_db=10.0,                # 8〜12dBで調整
        effects_profile_id=["small-bluetooth-speaker-class-device"],
    )
    voice = texttospeech.VoiceSelectionParams(
        language_code=LANG,
        name=DEFAULT_TTS_VOICE
    )
    req = texttospeech.SynthesizeSpeechRequest(
        input=texttospeech.SynthesisInput(text=text),
        voice=voice,
        audio_config=audio_cfg
    )
    wav = tts_client.synthesize_speech(request=req).audio_content
    # WAVヘッダが来る環境もあるので簡易剥がし
    if len(wav) >= 12 and wav[:4]==b'RIFF' and wav[8:12]==b'WAVE':
        i = 12
        while i+8 <= len(wav):
            cid = wav[i:i+4]; csz = int.from_bytes(wav[i+4:i+8],'little'); i += 8
            if cid == b'data': return wav[i:i+csz]
            i += csz + (csz & 1)
    return wav

def llm_reply_en(user_text: str) -> str:
    print(f"[LLM-REQ] {user_text}")
    r = oa.responses.create(
        model="gpt-4.1-nano",
        input=[
            {"role":"system","content":"You are a concise, friendly English voice assistant. Keep replies short and natural for TTS."},
            {"role":"user","content":user_text}
        ],
        max_output_tokens=120
    )
    try:
        out = r.output_text.strip()
    except Exception:
        out = r.output[0].content[0].text.strip()
    print(f"[LLM-RES] {out}")
    return out

# ===== STT Worker (configパラメータ + audio-only requests) =====
def google_streaming_worker(audio_q, result_q):
    recog_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code=LANG,
        enable_automatic_punctuation=True,
        # 互換性重視：環境によって latest_short が使えないことがあるため default に
        model="default",
    )
    stream_config = speech.StreamingRecognitionConfig(
        config=recog_config,
        interim_results=True,
        single_utterance=False,
    )

    # 最初の1フレームを受け取ってから開始（Audio Timeout回避）
    first = audio_q.get()
    if first is None:
        return

    def req_iter():
        # ★ ここでは "audio_content" だけを送る（streaming_config は送らない）
        #   → streaming_recognize(config=stream_config, requests=req_iter()) と組み合わせる
        yield speech.StreamingRecognizeRequest(audio_content=first)
        while True:
            chunk = audio_q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    try:
        print("[STT] streaming_recognize started (config param + audio-only requests)")
        # ★ config を引数で渡し、requests 側は audio のみ
        for resp in speech_client.streaming_recognize(config=stream_config, requests=req_iter()):
            for res in resp.results:
                alt = res.alternatives[0].transcript
                if res.is_final:
                    print(f"[FINAL] {alt}")
                    result_q.put((alt, time.time()))
                else:
                    print(f"[INTERIM] {alt}")
    except Exception as e:
        print("[stt stream error]", e)

# ===== WS helpers =====
async def send_pcm_frames(ws: WebSocket, pcm16: bytes):
    # 20msごとに分割してBase64テキストで送る
    for i in range(0, len(pcm16), FRAME_BYTES):
        chunk = pcm16[i:i+FRAME_BYTES]
        b64 = base64.b64encode(chunk).decode("ascii")
        await ws.send_text(b64)
        await asyncio.sleep(0)

@app.get("/", response_class=PlainTextResponse)
def hello():
    return "Realtime voice chatbot WS server (en-US)."

# ===== Main WS =====
@app.websocket("/ws_chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    print("[WS] connected")

    audio_q = None
    result_q = None
    worker = None
    worker_start = 0.0

    def start_worker():
        nonlocal audio_q, result_q, worker, worker_start
        audio_q = queue.Queue()
        result_q = queue.Queue()
        worker = threading.Thread(target=google_streaming_worker, args=(audio_q, result_q), daemon=True)
        worker.start()
        worker_start = time.time()
        print("[STT] worker started")

    def stop_worker():
        nonlocal worker
        if worker is not None and worker.is_alive():
            try:
                audio_q.put(None)
            except Exception:
                pass
            with contextlib.suppress(Exception):
                worker.join(timeout=2)
        worker = None
        print("[STT] worker stopped")

    # 最初は「聞く」状態から開始
    start_worker()

    # 状態
    silent_ms = 0
    speaking  = False
    pending_texts = []
    last_final_ts = None

    # 診断
    rx_frames = 0
    t0 = time.time()

    async def build_and_speak(user_text: str):
        """LLM→TTS→送出（非ブロッキング）。送出が終わったら次ターンのためにSTTを張り直す。"""
        nonlocal speaking
        try:
            speaking = True
            reply   = await asyncio.to_thread(llm_reply_en, user_text)
            pcm_tts = await asyncio.to_thread(synth_tts_16k_linear16, reply)
            print(f"[TTS] start, bytes={len(pcm_tts)} (~{len(pcm_tts)/FRAME_BYTES*20:.0f}ms)")
            await send_pcm_frames(ws, pcm_tts)
            print("[TTS] done")
        except Exception as e:
            print("[PIPE error]", e)
        finally:
            speaking = False
            # ★ 次の発話に備えて “聞く” を再開
            start_worker()

    # 任意：TTS中の保険（ネット揺らぎ時にゼロを補充）
    ka_running = True
    async def stt_keepalive():
        while ka_running:
            if speaking and worker is not None and worker.is_alive():
                try:
                    audio_q.put(b"\x00" * FRAME_BYTES, block=False)
                except Exception:
                    pass
            await asyncio.sleep(0.1)
    ka_task = asyncio.create_task(stt_keepalive())

    try:
        while True:
            msg = await ws.receive_text()
            rx_frames += 1
            if rx_frames % 50 == 0:
                dt = time.time() - t0
                fps = rx_frames / dt if dt > 0 else 0
                print(f"[RX] {rx_frames} frames / {dt:.2f}s  (~{fps:.1f} fps)")

            pcm = base64.b64decode(msg)

            # ★ STTへ供給（ワーカーが存在する時だけ）
            if worker is not None and worker.is_alive():
                audio_q.put(pcm)

            # 無音診断（ログ）
            r = rms_int16(pcm)
            silent_ms = silent_ms + 20 if r < RMS_THRESH else 0
            if rx_frames % 25 == 0:
                print(f"[RMS] {r:.0f}  [silence={silent_ms}ms]  thresh={RMS_THRESH}")

            # ワーカー死活監視（異常時は即再起動）
            if worker is not None and not worker.is_alive() and not speaking:
                print("[STT] worker died; restarting")
                start_worker()
                silent_ms = 0

            # STTから確定テキストを回収（FINAL受信＝即トリガ）
            got_new_final = False
            if worker is not None:
                while not result_q.empty():
                    txt, ts = result_q.get_nowait()
                    pending_texts.append(txt)
                    last_final_ts = ts
                    got_new_final = True

            # ★ 返答トリガ（FINAL直後：ここで“聞く”を一旦止める）
            trigger = False
            trig_reason = ""
            now = time.time()
            if pending_texts:
                if got_new_final:
                    trigger = True
                    trig_reason = "final-immediate"
                # フォールバック（念のため）
                elif silent_ms >= SILENCE_MS:
                    trigger = True
                    trig_reason = f"silence {SILENCE_MS}ms"
                elif last_final_ts is not None and (now - last_final_ts) * 1000 >= FINAL_WAIT_MS:
                    trigger = True
                    trig_reason = f"final-wait {FINAL_WAIT_MS}ms"

            if trigger and not speaking:
                user_text = " ".join(pending_texts).strip()
                pending_texts.clear()
                print(f"[TRIGGER] {trig_reason}  user='{user_text}'")

                # ★ 「話す」ターンに入る前に STT を明示的に停止（タイムアウト回避）
                stop_worker()

                if user_text:
                    asyncio.create_task(build_and_speak(user_text))
                silent_ms = 0

            # さらに保険：長無音や時間でのロールオーバー（“聞く”状態の時のみ）
            if worker is not None and worker.is_alive() and not speaking:
                # 長無音
                if silent_ms >= SEGMENT_SILENCE_MS:
                    print(f"[STT] segment: {SEGMENT_SILENCE_MS}ms silence -> restart stream")
                    stop_worker()
                    start_worker()
                    silent_ms = 0
                # 時間ロールオーバー
                if time.time() - worker_start >= STREAM_MAX_SEC:
                    print(f"[STT] segment: time rollover {STREAM_MAX_SEC}s -> restart stream")
                    stop_worker()
                    start_worker()

    except Exception as e:
        print("[WS error]", e)
    finally:
        # keepalive停止
        ka_running = False
        with contextlib.suppress(Exception):
            ka_task.cancel()

        stop_worker()
        await ws.close()
        print("[WS] disconnected")
