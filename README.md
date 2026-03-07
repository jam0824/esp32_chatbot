# esp32_chatbot

ESP32 を使った双方向リアルタイム音声チャットボットです。  
サーバー（Python / FastAPI）が Google STT・OpenAI LLM・Google TTS を連携し、ESP32 クライアントとの間で WebSocket 経由の音声ストリーミングを行います。

2つのクライアント構成があります:

| スケッチ | ボード | 特徴 |
|---|---|---|
| `esp32_realtime_full_duplex_oled` | ESP32-S3 など | OLED + I2S 音声入出力 |
| `esp32_supermini_servo` | **ESP32-C3 SuperMini** | OLED + I2S 音声入出力 + サーボモーター |

---

## サーバー側（Python / FastAPI）

### 1. 環境構築

```bash
# venv 作成 & 有効化
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

# 依存パッケージのインストール
pip install -r requirements.txt
pip install websockets   # uvicorn の WebSocket サポートに必要
```

### 2. 必要な環境変数

サーバー起動前に以下の環境変数を設定してください。

```bash
# OpenAI API キー
set OPENAI_API_KEY=sk-xxxxxxxxxxxxx

# Google Cloud サービスアカウント JSON のフルパス
set GOOGLE_APPLICATION_CREDENTIALS=C:\path\to\service-account.json
```

> **macOS / Linux** の場合は `set` を `export` に読み替えてください。

### 3. サーバー起動

```bash
python -m uvicorn server_chat_ws:app --host 0.0.0.0 --port 8000
```

サーバーは `ws://<サーバーIP>:8000/ws_chat` で WebSocket 接続を待ち受けます。

> **Windows の場合**: ファイアウォールでポート 8000 の受信を許可する必要があります。  
> 管理者 PowerShell で以下を実行してください:
> ```powershell
> New-NetFirewallRule -DisplayName "ESP32 Server Port 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -Profile Any
> ```

### 4. 主な設定（`server_chat_ws.py` 冒頭）

| 定数 | 既定値 | 説明 |
|---|---|---|
| `SILENCE_MS` | 300 | 返答トリガーまでの無音時間 (ms) |
| `VAD_AGGRESSIVENESS` | 3 | WebRTC VAD の厳しさ (0〜3) |
| `VAD_START_SPEECH_MS` | 100 | 発話開始判定に必要な連続音声 (ms) |
| `VAD_STOP_SILENCE_MS` | 400 | 発話終了判定の無音時間 (ms) |
| `DEFAULT_TTS_VOICE` | `en-US-Neural2-F` | Google TTS ボイス名 |
| `SYSTEM_PROMPT` | (英語教師 Chapiko) | LLM のシステムプロンプト |
| `LANG` | `en-US` | STT/TTS の言語コード |

---

## クライアント側（ESP32-C3 SuperMini + サーボ）

`esp32_supermini_servo/esp32_supermini_servo.ino`

### 1. 必要ライブラリ（Arduino IDE ライブラリマネージャ）

| ライブラリ | 用途 |
|---|---|
| ArduinoWebsockets | WebSocket 通信 |
| ArduinoJson | JSON パース |
| Adafruit GFX Library | グラフィック基盤 |
| Adafruit SSD1306 | OLED ドライバ |
| FluxGarage RoboEyes | 目のアニメーション |
| ESP32Servo | サーボモーター制御 |

### 2. Arduino IDE ボード設定

| 項目 | 設定値 |
|---|---|
| ボード | **ESP32C3 Dev Module** |
| USB CDC On Boot | **Enabled** |
| Upload Speed | 921600 |

> ⚠️ ESP32-C3 SuperMini はネイティブ USB を使用するため、**USB CDC On Boot を必ず Enabled** にしてください。Disabled だとシリアル出力が見えません。

### 3. ピン配置

| 機能 | ピン | 接続先 |
|---|---|---|
| I2C SDA (OLED) | GPIO 6 | SSD1306 SDA |
| I2C SCL (OLED) | GPIO 7 | SSD1306 SCL |
| I2S BCLK | GPIO 8 | INMP441 SCK / MAX98357A BCLK |
| I2S LRCLK | GPIO 9 | INMP441 WS / MAX98357A LRC |
| I2S DOUT | GPIO 10 | MAX98357A DIN |
| I2S DIN | GPIO 5 | INMP441 SD |
| サーボ | GPIO 1 | SG90 信号線 |

### 4. スケッチの設定変更

```cpp
const char* WIFI_SSID = "YOUR_SSID";       // WiFi の SSID
const char* WIFI_PASS = "YOUR_PASS";       // WiFi のパスワード
const char* WS_URL    = "ws://192.168.1.151:8000/ws_chat";  // サーバーの IP:ポート
```

### 5. ビルド & 書き込み

1. Arduino IDE でボード設定を上記の通りにする
2. WiFi / WS_URL を自身の環境に合わせて修正
3. **書き込み**（Upload）を実行
4. シリアルモニター（115200 baud）で以下が表示されれば成功:
   ```
   === BOOT ===
   [SETUP] OLED init...
   [SETUP] OLED OK
   [SETUP] Servo init...
   [SETUP] Servo OK
   [SETUP] WiFi connecting...
   WiFi OK
   IP: 192.168.x.x
   [SETUP] WS connected!
   [SETUP] Done!
   ```

### 6. ハードウェア構成

```
                    ESP32-C3 SuperMini
                   ┌──────────────────┐
  INMP441 ────────►│ GPIO5  (I2S DIN) │
  (マイク)         │ GPIO8  (BCLK)    │◄──── 共有クロック
                   │ GPIO9  (LRCLK)   │◄──── 共有クロック
  MAX98357A ◄──────│ GPIO10 (I2S DOUT)│
  (スピーカー)     │                  │
  SSD1306 OLED ◄───│ GPIO6  (SDA)     │
  (128x64)         │ GPIO7  (SCL)     │
  SG90 サーボ ◄────│ GPIO1  (PWM)     │
                   └──────────────────┘
```

---

## 動作の流れ

1. ESP32 が WiFi に接続し、サーバーへ WebSocket 接続
2. マイク音声を 20ms ごとに Base64 エンコードしてサーバーへ送信
3. サーバーが WebRTC VAD で音声区間を検出し、Google STT でテキスト化
4. OpenAI LLM（GPT-4.1-nano）で応答テキストを生成
5. Google TTS で音声合成し、Base64 で ESP32 へ返送
6. ESP32 が MAX98357A で音声再生、OLED の表情を変え、サーボを動かす

---

## トラブルシューティング

| 症状 | 原因・対処 |
|---|---|
| シリアルモニターに `ESP-ROM:...` しか出ない | USB CDC On Boot → **Enabled** に変更 |
| WiFi が繋がらない（status=6 が続く） | `WiFi.setTxPower(WIFI_POWER_8_5dBm)` を追加（SuperMini の電力問題） |
| WS connect FAILED | サーバーが起動しているか確認 / ファイアウォールでポート 8000 を許可 |
| 音声を認識しているのに返答が来ない | `websockets` パッケージがインストールされているか確認（`pip install websockets`） |
| サーボが動かない / 電源が落ちる | サーボの振り幅を小さくする / WiFi TX パワーを下げる |

---

## 主要ファイル

| ファイル | 説明 |
|---|---|
| `server_chat_ws.py` | FastAPI WebSocket サーバー（Google STT/TTS + OpenAI LLM） |
| `esp32_supermini_servo/esp32_supermini_servo.ino` | ESP32-C3 SuperMini クライアント（OLED + 音声 + サーボ） |
| `esp32_realtime_full_duplex_oled/esp32_realtime_full_duplex_oled.ino` | ESP32 クライアント（OLED + 音声のみ） |
| `requirements.txt` | サーバー側 Python 依存パッケージ |
