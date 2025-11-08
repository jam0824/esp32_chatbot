# esp32_chatbot

ESP32 を使った双方向音声チャットのサンプル一式です。`server_chat_ws.py` が音声入出力を扱う WebSocket サーバ、`esp32_realtime_full_duplex_oled/esp32_realtime_full_duplex_oled.ino` がフルデュプレックス音声入出力＆OLED 表示を行うクライアントになります。

---

## サーバー側（Python / FastAPI）

### 1. 依存関係のインストール
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 必要な環境変数
- `OPENAI_API_KEY` : OpenAI Responses API 用
- `GOOGLE_APPLICATION_CREDENTIALS` : Google Cloud Speech/TTS 用のサービスアカウント JSON へのフルパス

### 3. 主な設定（`server_chat_ws.py`）
- サンプルレート/各種しきい値は冒頭の定数で固定（例: `SILENCE_MS = 300`, `VAD_STOP_SILENCE_MS = 400` など）
- TTS 設定: `DEFAULT_TTS_VOICE = "en-US-Neural2-F"`
- LLM プロンプト: `SYSTEM_PROMPT` に「Chapiko が英会話の先生」として振る舞う指示が埋め込まれています

### 4. 起動
```bash
uvicorn server_chat_ws:app --host 0.0.0.0 --port 8000
```
サーバーは `ws://<サーバーIP>:8000/ws_chat` で待ち受けます。ログに `[TTS] start ...` が出れば Google TTS が機能しています。

---

## クライアント側（ESP32）

### 1. 依存ライブラリ
Arduino IDE で以下をインストールしてください。
- ArduinoWebsockets
- Adafruit GFX / Adafruit SSD1306
- FluxGarage RoboEyes
- 公式 ESP32 ボードパッケージ（ESP32-S3 等）

### 2. ファイルの主な設定
`esp32_realtime_full_duplex_oled/esp32_realtime_full_duplex_oled.ino`
- Wi-Fi 情報: `WIFI_SSID`, `WIFI_PASS`
- サーバ URL: `WS_URL`（例: `ws://192.168.1.151:8000/ws_chat`）
- I2S ピン: `I2S_BCLK=27`, `I2S_LRCLK=26`, `I2S_DOUT=25`, `I2S_DIN=34`
- OLED ピン: `OLED_SCL=22`, `OLED_SDA=21`
- サンプルレート: `SAMPLE_RATE = 16000`（20ms 每に 320 サンプル送受信）

### 3. ハードウェア構成
- INMP441 などの I2S マイク（24bit 左詰）を `I2S_DIN` に接続
- MAX98357A 等の I2S DAC を `I2S_DOUT` へ接続
- SSD1306 OLED（128x64）を I2C (`OLED_SCL`, `OLED_SDA`) へ

### 4. ビルド & 書き込み手順
1. Arduino IDE でボードを ESP32 系に設定
2. 上記設定値を自身のネットワーク/ハードウェアに合わせて修正
3. スケッチをビルドし、ESP32 へ書き込み
4. シリアルモニタで `WiFi OK` や `[WS] opened` が表示されればサーバ接続が成功

### 5. 動作
- ESP32 は 20ms ごとにマイク音声を Base64 でサーバへ送信
- サーバから返る音声（16bit PCM）は MAX98357A へ出力
- OLED は `RoboEyes` で表情を更新し、TTS 再生中は HAPPY 表情に切り替わります

---

## 主要ファイル
- `server_chat_ws.py` : FastAPI WebSocket サーバ（Google STT/TTS + OpenAI LLM）
- `esp32_realtime_full_duplex_oled/esp32_realtime_full_duplex_oled.ino` : ESP32 フルデュプレックス音声クライアント
- `requirements.txt` : サーバー側依存関係

---

## トラブルシュートメモ
- 音声が返らない場合: サーバログの `[TTS] start` / `[TTS] done` を確認し、`GOOGLE_APPLICATION_CREDENTIALS` の設定やネットワーク到達性をチェック。
- TTS ボイスの変更: `DEFAULT_TTS_VOICE` を Google Cloud の対応ボイスに変更。
- しきい値調整: `SILENCE_MS`, `VAD_STOP_SILENCE_MS` などを用途に応じてチューニングしてください。

以上の手順で、ESP32 と Python サーバを連携させたリアルタイム音声チャットが動作します。
