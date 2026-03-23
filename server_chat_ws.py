from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
import asyncio, base64, math, threading, queue, contextlib, time, os, json, re
from google.cloud import speech_v1 as speech
from openai import OpenAI
from starlette.websockets import WebSocketDisconnect, WebSocketState
import webrtcvad
from collections import deque
import httpx
from dotenv import load_dotenv

load_dotenv()

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

# STT / TTS（ElevenLabs は synth_tts_16k_linear16 内で環境変数参照）
LANG          = "ja-JP"
SYSTEM_PROMPT = "あなたの名前はチャピコです。簡潔でフレンドリーな日本語の音声アシスタントです。返答は音声合成に適した短く自然な文にしてください。50文字以内です。"

app = FastAPI()
speech_client = speech.SpeechClient()
oa            = OpenAI()  # OPENAI_API_KEY を環境変数に
history = ""

# ===== Utils =====
def collect_final_results(worker_alive: bool, result_q: queue.Queue, pending: list[str]) -> bool:
    """STT結果キューから最終テキストをpendingへ追加し、追加があればTrue。"""
    if result_q is None:
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

def _pcm_from_maybe_wav(wav: bytes) -> bytes:
    """WAV ラップ時は data チャンクだけ返す（生 PCM のときはそのまま）。"""
    if len(wav) >= 12 and wav[:4] == b"RIFF" and wav[8:12] == b"WAVE":
        i = 12
        while i + 8 <= len(wav):
            cid = wav[i : i + 4]
            csz = int.from_bytes(wav[i + 4 : i + 8], "little")
            i += 8
            if cid == b"data":
                return wav[i : i + csz]
            i += csz + (csz & 1)
    return wav


def synth_tts_16k_linear16(text: str) -> bytes:
    """ElevenLabs TTS → 16kHz LINEAR16 生 PCM（output_format=pcm_16000）。"""
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    voice_id = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    if not api_key or not voice_id:
        raise RuntimeError("ELEVENLABS_API_KEY と ELEVENLABS_VOICE_ID を .env などで設定してください")

    model_id = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    params: dict[str, str | int] = {"output_format": "pcm_16000"}
    lat = (os.environ.get("ELEVENLABS_OPTIMIZE_STREAMING_LATENCY") or "").strip()
    if lat:
        try:
            params["optimize_streaming_latency"] = int(lat)
        except ValueError:
            pass

    body: dict = {"text": text, "model_id": model_id}
    ja_norm = (os.environ.get("ELEVENLABS_APPLY_JA_LANG_NORM") or "").strip().lower()
    if ja_norm in ("1", "true", "yes", "on"):
        body["apply_language_text_normalization"] = True

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, params=params, json=body, headers=headers)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.text[:500]
            except Exception:
                pass
            raise RuntimeError(f"ElevenLabs TTS HTTP {e.response.status_code}: {detail}") from e
        raw = r.content

    return _pcm_from_maybe_wav(raw)

def llm_reply(user_text: str) -> str:
    global history
    print(f"[LLM-REQ] {user_text}")
    history = history + "User: " + user_text + "\n"
    system_content = SYSTEM_PROMPT + " chat history: " + history
    try:
        r = oa.responses.create(
            model="gpt-5-nano",
            instructions=system_content,
            input=[
                {"role":"user","content":user_text}
            ],
            max_output_tokens=1024,
            reasoning={"effort": "low"},
        )
        print(f"[LLM-RAW] status={r.status}  output={r.output}")
        out = r.output_text.strip()
    except Exception as e:
        print(f"[LLM-ERR] {type(e).__name__}: {e}")
        out = ""
    if not out:
        out = "すみません、うまく答えられませんでした。"
        print("[LLM-RES] (empty response, using fallback)")
    else:
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
    return "Realtime voice chatbot WS server (ja-JP, ElevenLabs TTS, WebRTC VAD-gated)."

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
        """LLM→文分割TTS→送出（非ブロッキング）。送出が終わったら次ターンのためにSTTを張り直す。"""
        nonlocal speaking
        nonlocal last_trigger_ts
        try:
            reply   = await asyncio.to_thread(llm_reply, user_text)
            # テキストもクライアントへ通知（JSON）
            try:
                await ws.send_text(json.dumps({"type": "text", "message": reply}))
            except Exception as e:
                print("[WS text send error]", e)

            # 空レスポンスならTTSスキップ
            if not reply or not reply.strip():
                print("[TTS] skipped (empty reply)")
                return

            # 文単位で分割して逐次 TTS → 送信（TTFA 短縮）
            list_sentences = [s for s in re.split(r'(?<=[。！？!?])', reply) if s.strip()]
            if not list_sentences:
                list_sentences = [reply]

            print(f"[TTS] split into {len(list_sentences)} sentence(s)")
            first_sent = True
            for idx, sentence in enumerate(list_sentences):
                pcm_tts = await asyncio.to_thread(synth_tts_16k_linear16, sentence)
                if first_sent and last_trigger_ts is not None:
                    tat = (time.time() - last_trigger_ts) * 1000.0
                    print(f"[TAT] first-audio {tat:.1f} ms (elevenlabs-tts)")
                    first_sent = False
                print(f"[TTS] sent [{idx+1}/{len(list_sentences)}] \"{sentence}\" bytes={len(pcm_tts)} (~{len(pcm_tts)/FRAME_BYTES*20:.0f}ms)")
                await send_pcm_frames(ws, pcm_tts)
            print("[TTS] done (all sentences)")
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
