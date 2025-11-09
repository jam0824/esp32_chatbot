from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
import asyncio, base64, math, threading, queue, contextlib, time, os, json
from google.cloud import speech_v1 as speech
from google.cloud import texttospeech
from openai import OpenAI
from starlette.websockets import WebSocketDisconnect, WebSocketState
import webrtcvad
from collections import deque

# ===== Config =====
SAMPLE_RATE   = 16000
FRAME_BYTES   = 640                 # 20ms (16k * 2B * 0.02s)

# 返答トリガ & ロールオーバー（低遅延寄りの既定。環境変数で上書き可）
SILENCE_MS          = 300
FINAL_WAIT_MS       = 200
STREAM_MAX_SEC      = 55   # 55秒ごとに保守的に張り直し（“聞く”状態の時だけ）
IDLE_STOP_MS        = 1200   # 受信アイドルでSTT停止するまでの時間(ms)

# --- WebRTC VAD パラメータ ---
# aggressiveness: 0(緩)〜3(厳)。数値が大きいほどノイズでも無音扱いになりやすい
VAD_AGGRESSIVENESS  = 3
# 発話開始とみなすのに必要な連続スピーチ時間
VAD_START_SPEECH_MS = 100   # ms
# 発話終了（無音）とみなしSTT停止するまでの連続無音時間（低遅延寄り）
VAD_STOP_SILENCE_MS = 400   # ms
# 発話の頭切れ防止のため、STT開始時に過去フレームを先送りするプリロール
VAD_PREBUFFER_MS    = 200      # ms

# STT/TTS
LANG                = "en-US"
DEFAULT_TTS_VOICE   = "en-US-Neural2-F"  # 例: en-US-Studio-O, en-US-Wavenet-D
SYSTEM_PROMPT       = "Your name is Chapiko.You are a concise, friendly English voice assistant. User studies English. You are a teacher.Keep replies short and natural for TTS."

app = FastAPI()
speech_client = speech.SpeechClient()
tts_client    = texttospeech.TextToSpeechClient()
oa            = OpenAI()  # OPENAI_API_KEY を環境変数に
history = ""

# ===== Utils =====
def collect_final_results(worker_alive: bool, result_q: queue.Queue, pending: list[str]) -> bool:
    """STT結果キューから最終テキストをpendingへ追加し、追加があればTrue。"""
    if not worker_alive:
        return False
    added = False
    while not result_q.empty():
        try:
            txt, _ = result_q.get_nowait()
        except queue.Empty:
            break
        if txt and txt.strip():
            pending.append(txt)
            added = True
    return added


def should_trigger_reply(
    pending_texts: list[str],
    got_new_final: bool,
    silence_streak: int,
    last_final_ts: float | None,
) -> tuple[bool, str]:
    """LLM 返答を開始するか判定し、トリガ状態と理由を返す。"""
    if not pending_texts:
        return False, ""

    now = time.time()
    if got_new_final:
        return True, "final-immediate"
    if silence_streak >= SILENCE_MS:
        return True, f"silence {SILENCE_MS}ms"
    if last_final_ts is not None and (now - last_final_ts) * 1000 >= FINAL_WAIT_MS:
        return True, f"final-wait {FINAL_WAIT_MS}ms"
    return False, ""

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
        speaking_rate=1.05,
        pitch=-2.0,
        volume_gain_db=10.0,
        effects_profile_id=["small-bluetooth-speaker-class-device"],
    )
    voice = texttospeech.VoiceSelectionParams(language_code=LANG, name=DEFAULT_TTS_VOICE)
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
    global history
    print(f"[LLM-REQ] {user_text}")
    history = history + "User: " + user_text + "\n"
    system_content = SYSTEM_PROMPT + " chat history: " + history
    r = oa.responses.create(
        model="gpt-4.1-nano",
        input=[
            {"role":"system","content":system_content},
            {"role":"user","content":user_text}
        ],
        max_output_tokens=120
    )
    try:
        out = r.output_text.strip()
    except Exception:
        out = r.output[0].content[0].text.strip()
    print(f"[LLM-RES] {out}")
    history = history + "Assistant: " + out + "\n"
    print(f"[LLM-HIST] {history}")
    return out

# ===== STT Worker (configパラメータ + audio-only requests) =====
def google_streaming_worker(audio_q, result_q):
    recog_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code=LANG,
        enable_automatic_punctuation=True,
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
        # ★ requests側は audio のみ（configは関数引数で指定）
        yield speech.StreamingRecognizeRequest(audio_content=first)
        while True:
            chunk = audio_q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    try:
        print("[STT] streaming_recognize started (config param + audio-only requests)")
        for resp in speech_client.streaming_recognize(config=stream_config, requests=req_iter()):
            for res in resp.results:
                alt = res.alternatives[0].transcript
                # 空INTERIM/FINALは完全スキップ
                if not alt or not alt.strip():
                    continue
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
    return "Realtime voice chatbot WS server (en-US, WebRTC VAD-gated)."

# ===== Main WS =====
@app.websocket("/ws_chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    print("[WS] connected")

    # --- VAD 準備 ---
    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    prebuffer_frames = max(1, VAD_PREBUFFER_MS // 20)
    preroll = deque(maxlen=prebuffer_frames)

    # STT ワーカー管理
    audio_q = None
    result_q = None
    worker  = None
    worker_start = 0.0

    def worker_alive() -> bool:
        return (worker is not None) and worker.is_alive()

    def start_worker():
        """STT ワーカーを生成・起動（既に動作中なら何もしない）。"""
        nonlocal audio_q, result_q, worker, worker_start
        if worker_alive():
            return
        audio_q = queue.Queue()
        result_q = queue.Queue()
        worker = threading.Thread(
            target=google_streaming_worker,
            args=(audio_q, result_q),
            daemon=True,
        )
        worker.start()
        worker_start = time.time()
        print("[STT] worker started")

    def stop_worker():
        """STT ワーカーを停止し、参照をクリアする。"""
        nonlocal worker
        if worker_alive():
            try:
                audio_q.put(None)
            except Exception:
                pass
            with contextlib.suppress(Exception):
                worker.join(timeout=2)
        worker = None
        print("[STT] worker stopped")

    # 最初はリスニング開始（VADで自動的にON/OFF）
    start_worker()

    # 状態
    speaking        = False          # サーバがTTS出力中なら True（この間はVADしても起こさない）
    pending_texts   = []
    last_final_ts   = None
    speech_streak   = 0              # 連続 is_speech=True 時間(ms)
    silence_streak  = 0              # 連続 is_speech=False 時間(ms)

    # 診断
    rx_frames = 0
    t0 = time.time()
    last_rx_ts = time.time()
    last_trigger_ts = None

    async def build_and_speak(user_text: str):
        """LLM→TTS→送出（非ブロッキング）。送出が終わったら次ターンのためにSTTを張り直す。"""
        nonlocal speaking
        nonlocal last_trigger_ts
        try:
            reply   = await asyncio.to_thread(llm_reply_en, user_text)
            # テキストもクライアントへ通知（JSON）
            try:
                await ws.send_text(json.dumps({"type": "text", "message": reply}))
            except Exception as e:
                print("[WS text send error]", e)
            pcm_tts = await asyncio.to_thread(synth_tts_16k_linear16, reply)
            if last_trigger_ts is not None:
                tat = (time.time() - last_trigger_ts) * 1000.0
                print(f"[TAT] first-audio {tat:.1f} ms (google)")
            print(f"[TTS] start, bytes={len(pcm_tts)} (~{len(pcm_tts)/FRAME_BYTES*20:.0f}ms)")
            await send_pcm_frames(ws, pcm_tts)
            print("[TTS] done")
        except Exception as e:
            print("[PIPE error]", e)
        finally:
            speaking = False
            # TTS終了後、次の発話に備えSTTを起こす（VADに任せたいならここを削ってもよい）
            start_worker()

    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                last_rx_ts = time.time()
            except asyncio.TimeoutError:
                # 受信が途切れて一定時間経過したらSTTを停止（Audio Timeout回避）
                if worker_alive() and (time.time() - last_rx_ts) * 1000 >= IDLE_STOP_MS:
                    print(f"[STT] idle >= {IDLE_STOP_MS}ms -> stop STT")
                    stop_worker()
                    preroll.clear()
                continue

            rx_frames += 1
            if rx_frames % 50 == 0:
                dt = time.time() - t0
                fps = rx_frames / dt if dt > 0 else 0
                print(f"[RX] {rx_frames} frames / {dt:.2f}s  (~{fps:.1f} fps)")

            pcm = base64.b64decode(msg)

            # --- WebRTC VAD 判定（16kHz/16bit/mono/20ms 必須）---
            speech = False
            if not speaking:
                try:
                    speech = vad.is_speech(pcm, SAMPLE_RATE)
                except Exception as e:
                    # 異常なフレーム長等は無音扱いに
                    speech = False

            # --- VAD 状態更新 ---
            if speech:
                speech_streak += 20
                silence_streak = 0
            else:
                silence_streak += 20
                speech_streak = 0

            # --- speaking 中の扱い：回り込み防止のため何もしない ---
            if speaking:
                # TTSの音がマイクに回り込んでもVADで起こさない
                preroll.clear()
                continue

            # --- リスニング時のVADゲート処理 ---
            if worker_alive():
                # STT稼働中：とりあえず連続性のためフレームは送る
                audio_q.put(pcm)

                # 無音が続けば停止
                if silence_streak >= VAD_STOP_SILENCE_MS:
                    print(f"[VAD] silence >= {VAD_STOP_SILENCE_MS}ms -> stop STT")
                    stop_worker()
                    preroll.clear()  # 次の起動に備えプリロールはクリア
            else:
                # STT停止中：プリロールに溜めつつ、一定の連続スピーチで起動
                preroll.append(pcm)
                if speech_streak >= VAD_START_SPEECH_MS:
                    print(f"[VAD] speech >= {VAD_START_SPEECH_MS}ms -> start STT (with {len(preroll)} preroll frames)")
                    start_worker()
                    # 起動直後は最初のフレーム待ちなので、プリロールを一括供給
                    try:
                        while preroll:
                            audio_q.put(preroll.popleft())
                    except Exception:
                        preroll.clear()
                    # 以降は通常どおり供給される

            # --- STTから確定テキストを回収（空は捨てる） ---
            got_new_final = collect_final_results(worker_alive(), result_q, pending_texts)
            if got_new_final:
                last_final_ts = time.time()

            # --- 返答トリガ（FINAL直後優先） ---
            trigger, trig_reason = should_trigger_reply(
                pending_texts=pending_texts,
                got_new_final=got_new_final,
                silence_streak=silence_streak,
                last_final_ts=last_final_ts,
            )

            if trigger:
                user_text = " ".join(pending_texts).strip()
                pending_texts.clear()
                print(f"[TRIGGER] {trig_reason}  user='{user_text}'")

                # 話す前にSTTを明示停止
                stop_worker()
                preroll.clear()

                if user_text:
                    speaking = True
                    last_trigger_ts = time.time()
                    asyncio.create_task(build_and_speak(user_text))

                # カウンタ類リセット
                speech_streak  = 0
                silence_streak = 0

            # --- “聞く”状態の時間ロールオーバー ---
            if worker_alive() and (time.time() - worker_start >= STREAM_MAX_SEC):
                print(f"[STT] segment: time rollover {STREAM_MAX_SEC}s -> restart stream")
                stop_worker()
                start_worker()

    except WebSocketDisconnect as e:
        print(f"[WS disconnect] code={getattr(e, 'code', None)} reason={getattr(e, 'reason', '')}")
    except Exception as e:
        print("[WS error]", e)
    finally:
        stop_worker()
        if getattr(ws, "application_state", None) != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await ws.close()
        print("[WS] disconnected")
