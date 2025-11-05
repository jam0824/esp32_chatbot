from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
import asyncio, base64, math, threading, queue, contextlib, time
from google.cloud import speech_v1 as speech
from google.cloud import texttospeech
from openai import OpenAI

SAMPLE_RATE   = 16000
FRAME_BYTES   = 640                 # 20ms (16k * 2B * 0.02s)
SILENCE_MS    = 600
RMS_THRESH    = 500                 # ← 環境がうるさいなら 800〜1200 に上げる or 下のログを見て調整
FINAL_WAIT_MS = 500                 # ★ FINAL確定後、この時間静かなら返答トリガ
LANG          = "ja-JP"

app = FastAPI()
speech_client = speech.SpeechClient()
tts_client    = texttospeech.TextToSpeechClient()
oa            = OpenAI()  # OPENAI_API_KEY

def rms_int16(pcm: bytes) -> float:
    import array
    a = array.array('h'); a.frombytes(pcm)
    if not a: return 0.0
    s = sum(x*x for x in a)
    return math.sqrt(s/len(a))

def synth_tts_16k_linear16(text: str) -> bytes:
    req = texttospeech.SynthesizeSpeechRequest(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(language_code=LANG),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE,
            volume_gain_db=8.0,
        )
    )
    wav = tts_client.synthesize_speech(request=req).audio_content
    if len(wav) >= 12 and wav[:4]==b'RIFF' and wav[8:12]==b'WAVE':
        i = 12
        while i+8 <= len(wav):
            cid = wav[i:i+4]; csz = int.from_bytes(wav[i+4:i+8],'little'); i += 8
            if cid == b'data': return wav[i:i+csz]
            i += csz + (csz & 1)
    return wav

def llm_reply_ja(user_text: str) -> str:
    # コンソールに LLM 入出力を必ず出す（★）
    print(f"[LLM-REQ] {user_text}")
    r = oa.responses.create(
        model="gpt-4.1-nano",
        input=[
            {"role":"system","content":"あなたは日本語の音声会話アシスタント。返答は短く、自然に。"},
            {"role":"user","content":user_text}
        ],
        max_output_tokens=160
    )
    try:
        out = r.output_text.strip()
    except Exception:
        out = r.output[0].content[0].text.strip()
    print(f"[LLM-RES] {out}")  # ★ 出力をログ
    return out

def google_streaming_worker(audio_q: "queue.Queue[bytes|None]",
                            result_q: "queue.Queue[tuple[str,float]]"):  # ★ (text, timestamp)
    recog_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code=LANG,
        enable_automatic_punctuation=True,
        model="default",
    )
    stream_config = speech.StreamingRecognitionConfig(
        config=recog_config, interim_results=True, single_utterance=False
    )

    first = audio_q.get()
    if first is None:
        return

    def req_iter():
        yield speech.StreamingRecognizeRequest(audio_content=first)
        while True:
            chunk = audio_q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    try:
        for resp in speech_client.streaming_recognize(config=stream_config, requests=req_iter()):
            for res in resp.results:
                alt = res.alternatives[0].transcript
                if res.is_final:
                    print(f"[FINAL] {alt}")
                    result_q.put((alt, time.time()))  # ★ FINALの時刻も渡す
                else:
                    print(f"[INTERIM] {alt}")
    except Exception as e:
        print("[stt stream error]", e)

async def send_pcm_frames(ws: WebSocket, pcm16: bytes):
    for i in range(0, len(pcm16), FRAME_BYTES):
        chunk = pcm16[i:i+FRAME_BYTES]
        b64 = base64.b64encode(chunk).decode("ascii")
        await ws.send_text(b64)
        await asyncio.sleep(0)  # イベントループに譲る

@app.get("/", response_class=PlainTextResponse)
def hello():
    return "Realtime voice chatbot WS server."

@app.websocket("/ws_chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    print("[WS] connected")

    audio_q: "queue.Queue[bytes|None]" = queue.Queue()
    result_q: "queue.Queue[tuple[str,float]]" = queue.Queue()
    worker = threading.Thread(target=google_streaming_worker, args=(audio_q, result_q), daemon=True)
    worker.start()

    silent_ms = 0
    speaking  = False
    pending_texts: list[str] = []
    last_final_ts: float | None = None

    rx_frames = 0
    t0 = time.time()

    async def play_and_flag(pcm: bytes):
        nonlocal speaking
        try:
            print(f"[TTS] start, bytes={len(pcm)} (~{len(pcm)/FRAME_BYTES*20:.0f}ms)")
            await send_pcm_frames(ws, pcm)
            print("[TTS] done")
        except Exception as e:
            print("[TTS error]", e)
        finally:
            speaking = False

    try:
        while True:
            msg = await ws.receive_text()
            rx_frames += 1
            if rx_frames % 50 == 0:
                dt = time.time() - t0
                fps = rx_frames / dt if dt > 0 else 0
                print(f"[RX] {rx_frames} frames / {dt:.2f}s  (~{fps:.1f} fps)")

            pcm = base64.b64decode(msg)

            # STTへ供給：TTS中は無音を入れてタイムアウト回避
            audio_q.put(pcm if not speaking else b"\x00" * FRAME_BYTES)

            # 無音検出（診断ログ）
            r = rms_int16(pcm)
            silent_ms = silent_ms + 20 if r < RMS_THRESH else 0
            if rx_frames % 25 == 0:
                print(f"[RMS] {r:.0f}  [silence={silent_ms}ms]  thresh={RMS_THRESH}")

            # STTから確定テキストを回収（FINAL受信＝即トリガ）
            got_new_final = False
            while not result_q.empty():
                txt, ts = result_q.get_nowait()
                pending_texts.append(txt)
                last_final_ts = ts
                got_new_final = True

            # 返答トリガ条件
            trigger = False
            trig_reason = ""
            now = time.time()

            if not speaking and pending_texts:
                if got_new_final:
                    trigger = True
                    trig_reason = "final-immediate"
                elif silent_ms >= SILENCE_MS:
                    trigger = True
                    trig_reason = f"silence {SILENCE_MS}ms"
                elif last_final_ts is not None and (now - last_final_ts) * 1000 >= FINAL_WAIT_MS:
                    trigger = True
                    trig_reason = f"final-wait {FINAL_WAIT_MS}ms"

            if trigger:
                user_text = " ".join(pending_texts).strip()
                pending_texts.clear()
                print(f"[TRIGGER] {trig_reason}  user='{user_text}'")
                if user_text:
                    speaking = True
                    try:
                        reply = llm_reply_ja(user_text)   # LLM入出力は関数内でログ済み
                        pcm_tts = synth_tts_16k_linear16(reply)
                        asyncio.create_task(play_and_flag(pcm_tts))  # 送出は並行
                    except Exception as e:
                        print("[PIPE error]", e)
                silent_ms = 0

    except Exception as e:
        print("[WS error]", e)
    finally:
        try: audio_q.put(None)
        except: pass
        with contextlib.suppress(Exception):
            worker.join(timeout=5)
        await ws.close()
        print("[WS] disconnected")
