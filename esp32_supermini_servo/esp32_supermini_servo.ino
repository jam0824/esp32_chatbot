#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <ArduinoWebsockets.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <FluxGarage_RoboEyes.h> // https://github.com/FluxGarage/RoboEyes
#include <ESP32Servo.h>

#include "esp_system.h"
#include "driver/i2s.h"
#include "mbedtls/base64.h"

using namespace websockets;

// =====================
// OLED / RoboEyes
// =====================
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET    -1
#define OLED_ADDR     0x3C
#define OLED_FPS 30
constexpr int FACE_INTERVAL_FRAMES = 500;

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
RoboEyes<Adafruit_SSD1306> eyes(display);

static int count = 0;
static volatile bool is_speak_face = false;
static uint32_t last_tts_ms = 0;

// =====================
// WiFi / WS
// =====================
const char* WIFI_SSID = "TP-Link_C4D5";
const char* WIFI_PASS = "75758141";
const char* WS_URL    = "ws://192.168.1.151:8000/ws_chat"; // <-- change

WebsocketsClient ws;

// =====================
// Audio
// =====================
static const int SAMPLE_RATE     = 16000;
static const int FRAME_SAMPLES   = SAMPLE_RATE / 50;     // 20ms -> 320
static const int FRAME_BYTES_16  = FRAME_SAMPLES * 2;    // 640 bytes
static const int FRAME_BYTES_32  = FRAME_SAMPLES * 4;    // 1280 bytes (mono 32-bit)

// ---- Pins (ESP32-C3 SuperMini wiring) ----
// I2C (OLED)
#define OLED_SDA 6
#define OLED_SCL 7

// I2S (shared clocks)
#define I2S_PORT       I2S_NUM_0
#define I2S_BCLK       8   // BCLK / SCK
#define I2S_LRCLK      9   // LRCLK / WS
#define I2S_DOUT       10  // to MAX98357A DIN
#define I2S_DIN        5   // from INMP441 SD

// =====================
// Servo (move only while assistant speaks)
// =====================
#define SERVO_PIN 1

Servo servo1;
static int servoAngle = 90;
static int servoDir   = 1;

// 小さめの振り幅にすると電源が落ちにくい
static const int SERVO_MIN = 60;
static const int SERVO_MAX = 120;
static const int SERVO_STEP = 3;
static const uint32_t SERVO_INTERVAL_MS = 20;
static uint32_t lastServo_ms = 0;

// 「喋っている」判定：最後にTTS音声フレームを受信してからこの時間以内
static const uint32_t TTS_ACTIVE_MS = 300;      // サーボ/マイク停止に使う
static const uint32_t FACE_ACTIVE_MS = 700;     // 顔は少しだけ余韻を持たせる（好みで調整）

// =====================
// Work buffers
// =====================
static uint8_t  rx32_buf[FRAME_BYTES_32];   // mic read (32-bit mono)
static int16_t  tx16_buf[FRAME_SAMPLES];    // inbound TTS (decoded here)
static int32_t  tx32_buf[FRAME_SAMPLES];    // expand to 32-bit for TX
static int16_t  pcm16_buf[FRAME_SAMPLES];   // mic -> server (16-bit)

// Base64 TX buffer (640 bytes -> 856 chars + '\0')
static char b64_tx[900];

// =====================
// I2S init
// =====================
void i2s_init_fulldup() {
  i2s_config_t cfg;
  memset(&cfg, 0, sizeof(cfg));
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX);
  cfg.sample_rate = SAMPLE_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;   // 32bit frame
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;    // mono (Left slot)
  cfg.communication_format = I2S_COMM_FORMAT_I2S;    // standard I2S
  cfg.intr_alloc_flags = 0;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len   = FRAME_SAMPLES;                 // 320
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = true;

  i2s_pin_config_t pins;
  memset(&pins, 0, sizeof(pins));
  pins.mck_io_num   = I2S_PIN_NO_CHANGE;
  pins.bck_io_num   = I2S_BCLK;
  pins.ws_io_num    = I2S_LRCLK;
  pins.data_out_num = I2S_DOUT;
  pins.data_in_num  = I2S_DIN;

  i2s_driver_install(I2S_PORT, &cfg, 0, NULL);
  i2s_set_pin(I2S_PORT, &pins);
  i2s_set_clk(I2S_PORT, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_MONO);
  i2s_zero_dma_buffer(I2S_PORT);
}

// INMP441(32bitフレームに入ってくる24bit相当) → 16bitへ
// ここは「>>16」が安定（>>8だとクリップしやすいことが多い）
static inline void convert_24to16(const uint8_t* in32, int16_t* out16, size_t samples) {
  const int32_t* p32 = reinterpret_cast<const int32_t*>(in32);
  for (size_t i = 0; i < samples; ++i) {
    int32_t v = p32[i] >> 16;     // ★ 重要：>>16
    out16[i] = (int16_t)v;
  }
}

// 16bit → 32bit左詰（MAX98357Aへ）
static inline void expand_16to32_left(const int16_t* in16, int32_t* out32, size_t samples) {
  for (size_t i = 0; i < samples; ++i) {
    out32[i] = ((int32_t)in16[i]) << 16;
  }
}

// =====================
// UI helper
// =====================
void reset_face(){
  eyes.setPosition(DEFAULT);
  eyes.setIdleMode(false);
  eyes.setCuriosity(false);
}

bool try_handle_text(const String& msg) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, msg);
  if (err) return false;
  const char* type = doc["type"] | "";
  if (String(type) != "text") return false;
  const char* message = doc["message"] | "";
  Serial.print("[TEXT] ");
  Serial.println(message);
  return true;
}

void start_oled(){
  Wire.begin(OLED_SDA, OLED_SCL);
  if(!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)){
    Serial.println("SSD1306 allocation failed.");
    for(;;);
  }
  display.clearDisplay();
  display.display();

  eyes.begin(SCREEN_WIDTH, SCREEN_HEIGHT, OLED_FPS);
  eyes.setDisplayColors(0, 1);
  eyes.setAutoblinker(true,3,1);
  eyes.setMood(DEFAULT);
}

void change_face(int& face_count){
  face_count++;
  if(face_count == FACE_INTERVAL_FRAMES){
    int r = random(4);
    switch(r){
      case 0: eyes.anim_laugh(); break;
      case 1: eyes.anim_confused(); break;
      case 2: eyes.setCuriosity(true); break;
      case 3: eyes.setIdleMode(true); break;
    }
    face_count = 0;
  }
}

// =====================
// Speaking detection (from incoming TTS audio)
// =====================
static inline bool ttsActive(uint32_t tailMs) {
  uint32_t now = millis();
  if (last_tts_ms == 0) return false;
  return (now - last_tts_ms) < tailMs;
}

// =====================
// Servo update
// =====================
void servo_init() {
  servo1.setPeriodHertz(50);
  servo1.attach(SERVO_PIN, 500, 2400);
  servo1.write(90);
  servoAngle = 90;
  servoDir = 1;
}

void servo_update(bool speaking) {
  uint32_t now = millis();
  if (now - lastServo_ms < SERVO_INTERVAL_MS) return;
  lastServo_ms = now;

  if (!speaking) {
    // ゆっくりセンターへ戻す（好みで）
    if (servoAngle < 90) servoAngle++;
    else if (servoAngle > 90) servoAngle--;
    servo1.write(servoAngle);
    return;
  }

  servoAngle += servoDir * SERVO_STEP;
  if (servoAngle >= SERVO_MAX) { servoAngle = SERVO_MAX; servoDir = -1; }
  if (servoAngle <= SERVO_MIN) { servoAngle = SERVO_MIN; servoDir = +1; }
  servo1.write(servoAngle);
}

// =====================
// Send mic frame (base64)  ※new/delete無し版
// =====================
void send_frame_base64(const uint8_t* pcm16, size_t len) {
  if (!ws.available()) return;

  size_t olen = 0;
  // 末尾の'\0'分を引く
  if (mbedtls_base64_encode((unsigned char*)b64_tx, sizeof(b64_tx) - 1, &olen, pcm16, len) == 0 && olen > 0) {
    b64_tx[olen] = '\0';
    ws.send(b64_tx); // text frame
  }
}

// =====================
// WS callbacks
// =====================
void onMessage(WebsocketsMessage msg) {
  if (!msg.isText()) return;

  String payload = msg.data();

  // JSON text?
  if (payload.length() > 0 && payload[0] == '{') {
    if (try_handle_text(payload)) return;
  }

  // Otherwise: base64 audio chunk
  // ★毎チャンク更新（これが重要：終端判定とサーボに効く）
  last_tts_ms = millis();

  // speaking face set
  if(!is_speak_face){
    reset_face();
    is_speak_face = true;
    eyes.setMood(HAPPY);
    eyes.update();
    Serial.println("[FACE] speaking");
  }

  // Base64 -> PCM16 (decode directly into tx16_buf)
  size_t olen = 0;
  int ret = mbedtls_base64_decode(
      (unsigned char*)tx16_buf, FRAME_BYTES_16, &olen,
      (const unsigned char*)payload.c_str(), payload.length()
  );
  if (ret != 0 || olen == 0) return;

  size_t samples = olen / 2;
  if (samples > FRAME_SAMPLES) samples = FRAME_SAMPLES;

  // 16bit → 32bit and play
  expand_16to32_left(tx16_buf, tx32_buf, samples);
  size_t w = 0;
  i2s_write(I2S_PORT, tx32_buf, samples * 4, &w, portMAX_DELAY);
}

void onEvent(WebsocketsEvent ev, String data){
  if (ev == WebsocketsEvent::ConnectionOpened) {
    Serial.println("[WS] opened");
  } else if (ev == WebsocketsEvent::ConnectionClosed) {
    Serial.println("[WS] closed");
  }
}

// =====================
// Setup / Loop
// =====================
void setup() {
  Serial.begin(115200);
  delay(1000);  // シリアル接続安定待ち
  Serial.println("\n=== BOOT ===");

  randomSeed(esp_random());

  Serial.println("[SETUP] OLED init...");
  start_oled();
  Serial.println("[SETUP] OLED OK");

  Serial.println("[SETUP] Servo init...");
  servo_init();
  Serial.println("[SETUP] Servo OK");

  Serial.println("[SETUP] WiFi scan...");
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_8_5dBm);  // ★ SuperMini電力問題対策
  delay(100);

  int n = WiFi.scanNetworks();
  Serial.print("[SCAN] Found ");
  Serial.print(n);
  Serial.println(" networks:");
  for (int i = 0; i < n; i++) {
    Serial.print("  ");
    Serial.print(i + 1);
    Serial.print(": ");
    Serial.print(WiFi.SSID(i));
    Serial.print(" (");
    Serial.print(WiFi.RSSI(i));
    Serial.print(" dBm) ch=");
    Serial.print(WiFi.channel(i));
    Serial.print(" ");
    Serial.println((WiFi.encryptionType(i) == WIFI_AUTH_OPEN) ? "open" : "encrypted");
  }
  WiFi.scanDelete();

  Serial.println("[SETUP] WiFi connecting...");
  WiFi.disconnect(true);
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int wifi_retry = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    wifi_retry++;
    if (wifi_retry % 10 == 0) {
      // WiFi status: 0=IDLE, 1=NO_SSID_AVAIL, 2=SCAN_COMPLETED,
      //   3=CONNECTED, 4=CONNECT_FAILED, 5=CONNECTION_LOST, 6=DISCONNECTED
      Serial.print(" [status=");
      Serial.print(WiFi.status());
      Serial.print(", retry=");
      Serial.print(wifi_retry);
      Serial.println("]");
    }
    if (wifi_retry >= 60) {
      Serial.println("\n[WiFi] Failed! Restarting...");
      ESP.restart();
    }
  }
  Serial.println("\nWiFi OK");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());

  Serial.println("[SETUP] I2S init...");
  i2s_init_fulldup();
  Serial.println("[SETUP] I2S OK");

  Serial.println("[SETUP] WS connecting...");
  Serial.print("  URL: ");
  Serial.println(WS_URL);
  ws.onMessage(onMessage);
  ws.onEvent(onEvent);
  bool ws_ok = ws.connect(WS_URL);
  if (ws_ok) {
    Serial.println("[SETUP] WS connected!");
  } else {
    Serial.println("[SETUP] WS connect FAILED! Server may be down.");
  }
  Serial.println("[SETUP] Done!");
}

void loop() {
  // WS受信を先に回す（音声再生を優先）
  ws.poll();

  const bool speakingForServo = ttsActive(TTS_ACTIVE_MS);
  const bool speakingForFace  = ttsActive(FACE_ACTIVE_MS);

  // サーボ更新（喋っている間だけ左右ゆっくり）
  servo_update(speakingForServo);

  // 喋っていない時だけマイクを送る（回り込み＆負荷低減）
  if (!speakingForServo && ws.available()) {
    size_t bytes_read = 0;
    i2s_read(I2S_PORT, rx32_buf, sizeof(rx32_buf), &bytes_read, portMAX_DELAY);
    if (bytes_read == sizeof(rx32_buf)) {
      convert_24to16(rx32_buf, pcm16_buf, FRAME_SAMPLES);
      send_frame_base64((const uint8_t*)pcm16_buf, FRAME_BYTES_16);
    }
  } else {
    // 喋っている間はWSを回しやすくする
    delay(1);
  }

  // 顔：喋り終わったら戻す
  if (is_speak_face && !speakingForFace) {
    is_speak_face = false;
    eyes.setMood(DEFAULT);
    Serial.println("[FACE] default");
  }

  // idle顔変化は「喋ってない時だけ」にする（好み）
  if (!is_speak_face) {
    change_face(count);
  }

  eyes.update();
}