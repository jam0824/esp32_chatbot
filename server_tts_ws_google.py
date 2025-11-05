# server_tts_ws_google.py
from fastapi import FastAPI, WebSocket
from google.cloud import texttospeech
import base64

SAMPLE_RATE = 16000
FRAME_BYTES = 640  # 16kHz * 2byte * 20ms

app = FastAPI()
tts = texttospeech.TextToSpeechClient()

def extract_wav_pcm(wav: bytes) -> bytes:
    if len(wav) < 12 or wav[:4] != b'RIFF' or wav[8:12] != b'WAVE':
        return wav  # 既に生PCMならそのまま
    i, n = 12, len(wav)
    while i + 8 <= n:
        cid = wav[i:i+4]
        csz = int.from_bytes(wav[i+4:i+8], 'little')
        i += 8
        if cid == b'data':
            return wav[i:i+csz]
        i += csz + (csz & 1)
    return wav

@app.websocket("/ws_tts")
async def ws_tts(ws: WebSocket):
    await ws.accept()
    await ws.send_bytes(b"\x00\x00" * int(SAMPLE_RATE * 0.2))
    text = "テストです。こちらの音声は聞こえますか？"

    req = texttospeech.SynthesizeSpeechRequest(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(language_code="ja-JP"),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE
        )
    )
    resp = tts.synthesize_speech(request=req)
    pcm = extract_wav_pcm(resp.audio_content)  # ★ ヘッダ除去

    # 20ms=640Bで分割送信
    for i in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[i:i+FRAME_BYTES]
        b64 = base64.b64encode(chunk).decode("ascii")
        await ws.send_text(b64)  # ★ テキストで送る
