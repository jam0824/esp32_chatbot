# server_stt_ws_google.py  (fixed)
from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse
import asyncio, base64, time, math, contextlib, threading, queue
from google.cloud import speech_v1 as speech

SAMPLE_RATE = 16000
FRAME_BYTES = 640                # 20ms (16k * 2B * 0.02s)
SILENCE_MS  = 600                # 区切り
RMS_THRESH  = 500                # 無音しきい値（環境で調整）
LANG        = "ja-JP"

app = FastAPI()
speech_client = speech.SpeechClient()

def rms_int16(pcm: bytes) -> float:
    import array
    a = array.array('h')
    a.frombytes(pcm)
    if len(a) == 0: return 0.0
    s = sum(x*x for x in a)
    return math.sqrt(s/len(a))

def make_request_iter(q: "queue.Queue[bytes|None]", stream_config):
    # 最初に設定パケット
    yield speech.StreamingRecognizeRequest(streaming_config=stream_config)
    while True:
        chunk = q.get()
        if chunk is None:
            break
        yield speech.StreamingRecognizeRequest(audio_content=chunk)

def run_streaming_recognize_thread(q: "queue.Queue[bytes|None]", phrase_prefix=""):
    import inspect
    # バージョン・署名のダンプ（起動時に1回だけ表示されればOK）
    try:
        print("[diag] SpeechClient.streaming_recognize signature:",
              inspect.signature(speech.SpeechClient.streaming_recognize))
    except Exception as _:
        pass

    recog_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code="ja-JP",
        enable_automatic_punctuation=True,
        model="default",   # 必要に応じて latest_long 等
    )
    stream_config = speech.StreamingRecognitionConfig(
        config=recog_config,
        interim_results=True,
        single_utterance=False,
    )

    def audio_iter():
        while True:
            chunk = q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    try:
        # ★ 必ず config=..., requests=... の両方を指定
        responses = speech_client.streaming_recognize(
            config=stream_config,
            requests=audio_iter(),
        )
        for resp in responses:
            for result in resp.results:
                txt = result.alternatives[0].transcript
                if result.is_final:
                    print(f"[FINAL] {phrase_prefix}{txt}")
                else:
                    print(f"[INTERIM] {phrase_prefix}{txt}")
    except Exception as e:
        print(f"[stream error] {e}")

@app.get("/", response_class=PlainTextResponse)
def hello():
    return "STT WebSocket server running."

@app.websocket("/ws_stt")
async def ws_stt(ws: WebSocket):
    await ws.accept()
    print("[WS] client connected")

    # 区切りごとに作り直すキュー＆スレッド
    audio_q: "queue.Queue[bytes|None]" = queue.Queue()
    worker = threading.Thread(target=run_streaming_recognize_thread, args=(audio_q,))
    worker.daemon = True
    worker.start()

    silent_ms = 0

    try:
        while True:
            msg = await ws.receive_text()   # Base64 テキスト
            pcm = base64.b64decode(msg)

            # Google へ供給
            audio_q.put(pcm)

            # 無音判定
            r = rms_int16(pcm)
            #print(f"[RMS] {r:.1f}")   # ← 追記（多すぎるなら10フレームに1回でもOK）
            if r < RMS_THRESH:
                silent_ms += 20
            else:
                silent_ms = 0

            # 600ms 連続無音でセグメント切り替え（ストリーム再生成）
            if silent_ms >= SILENCE_MS:
                print("[SEGMENT] --- 600ms silence ---")
                # 現ストリーム終了シグナル
                audio_q.put(None)
                # 終了待ち
                worker.join(timeout=5)

                # 新ストリーム
                silent_ms = 0
                audio_q = queue.Queue()
                worker = threading.Thread(target=run_streaming_recognize_thread, args=(audio_q,))
                worker.daemon = True
                worker.start()

    except Exception as e:
        print(f"[WS error] {e}")
    finally:
        # クリーンアップ
        try:
            audio_q.put(None)
        except Exception:
            pass
        with contextlib.suppress(Exception):
            worker.join(timeout=5)
        await ws.close()
        print("[WS] client disconnected")
