#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <ArduinoWebsockets.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <FluxGarage_RoboEyes.h> //https://github.com/FluxGarage/RoboEyes
#include "esp_system.h"
#include "driver/i2s.h"
#include "mbedtls/base64.h"
using namespace websockets;

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET    -1
#define OLED_ADDR     0x3C   // 必要なら 0x3D に
#define OLED_SCL 22
#define OLED_SDA 21
#define OLED_FPS 30
constexpr int FACE_INTERVAL_FRAMES = 500;

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
RoboEyes<Adafruit_SSD1306> eyes(display);
int count = 0;
static volatile bool is_speak_face = false;
static uint32_t last_tts_ms = 0;

String lastText;
static uint32_t lastText_ms = 0;
static const uint32_t TEXT_DISPLAY_MS = 7000;
static bool textActive = false;
static const int TEXT_AREA_Y = 48;

// ===== WiFi / WS =====
const char* WIFI_SSID = "TP-Link_C4D5";
const char* WIFI_PASS = "";
const char* WS_URL    = "ws://192.168.1.151:8000/ws_chat";

// ===== Audio =====
static const int SAMPLE_RATE     = 16000;
static const int FRAME_SAMPLES   = SAMPLE_RATE / 50;     // 20ms -> 320 samples
static const int FRAME_BYTES_16  = FRAME_SAMPLES * 2;    // 640 bytes
static const int FRAME_BYTES_32  = FRAME_SAMPLES * 4;    // 1280 bytes

// I2S pins (shared clock lines)
#define I2S_PORT       I2S_NUM_0
#define I2S_BCLK       27   // BCLK / SCK
#define I2S_LRCLK      26   // LRCLK / WS
#define I2S_DOUT       25   // to MAX98357A DIN
#define I2S_DIN        34   // from INMP441 SD

// Work buffers
static uint8_t  rx32_buf[FRAME_BYTES_32];  // mic read (32-bit)
static int16_t  tx16_buf[FRAME_SAMPLES];   // inbound TTS (16-bit)
static int32_t  tx32_buf[FRAME_SAMPLES];   // expand to 32-bit for TX
static int16_t  pcm16_buf[FRAME_SAMPLES];  // mic -> server (16-bit)

WebsocketsClient ws;

void i2s_init_fulldup() {
  i2s_config_t cfg;
  memset(&cfg, 0, sizeof(cfg));
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX);
  cfg.sample_rate = SAMPLE_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;      // 32bit frame (TX/RX共通)
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;       // Lchのみ
  cfg.communication_format = I2S_COMM_FORMAT_I2S;       // 標準I2S(MSB)
  cfg.intr_alloc_flags = 0;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len   = 256;
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
}

// 32bit(24bit左詰: INMP441) → 16bit 変換（8bit右シフト）
static inline void convert_24to16(const uint8_t* in32, int16_t* out16, size_t samples) {
  const int32_t* p32 = reinterpret_cast<const int32_t*>(in32);
  for (size_t i = 0; i < samples; ++i) {
    int32_t v = p32[i] >> 8;                // 24→16
    if (v > 32767) v = 32767; if (v < -32768) v = -32768;
    out16[i] = (int16_t)v;
  }
}

// 16bit → 32bit左詰（MAX98357Aへ）
static inline void expand_16to32_left(const int16_t* in16, int32_t* out32, size_t samples) {
  for (size_t i = 0; i < samples; ++i) {
    out32[i] = ((int32_t)in16[i]) << 16;   // ★ 8 → 16 に変更
  }
}

void send_frame_base64(const uint8_t* pcm16, size_t len) {
  size_t out_len = ((len + 2) / 3) * 4 + 8;
  std::unique_ptr<unsigned char[]> b64(new unsigned char[out_len]);
  size_t olen = 0;
  if (mbedtls_base64_encode(b64.get(), out_len, &olen, pcm16, len) == 0 && olen > 0) {
    ws.send(String((const char*)b64.get(), olen));
  }
}

void show_text_on_oled(const String& text) {
  lastText = text;
  lastText_ms = millis();
  textActive = true;
}

bool try_handle_text(const String& msg) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, msg);
  if (err) return false;
  const char* type = doc["type"] | "";
  if (String(type) != "text") return false;
  const char* message = doc["message"] | "";
  lastText = String(message);
  show_text_on_oled(lastText);
  Serial.print("[TEXT] ");
  Serial.println(lastText);
  return true;
}

void onMessage(WebsocketsMessage msg) {
  if (!msg.isText()) return;
  String payload = msg.data();
  if (try_handle_text(payload)) {
    return;
  }
  String b64 = payload;

  if(!is_speak_face){
    reset_face();
    is_speak_face = true;
    eyes.setMood(HAPPY);
    eyes.update();
    Serial.println("\nChange Speaking Face.");
    last_tts_ms = millis();
  }

  // Base64 -> 16bit PCM
  size_t out_len = (b64.length() * 3) / 4 + 8;
  std::unique_ptr<uint8_t[]> buf(new uint8_t[out_len]);
  size_t olen = 0;
  if (mbedtls_base64_decode(buf.get(), out_len, &olen,
                            (const unsigned char*)b64.c_str(), b64.length()) == 0 && olen > 0) {
    // 16bit→32bitに拡張して送出
    size_t samples = olen / 2;
    if (samples > FRAME_SAMPLES) samples = FRAME_SAMPLES; // 安全策
    memcpy(tx16_buf, buf.get(), samples * 2);
    expand_16to32_left(tx16_buf, tx32_buf, samples);
    size_t w = 0;
    i2s_write(I2S_PORT, tx32_buf, samples * 4, &w, portMAX_DELAY);
  }
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

void loop_oled_idle(){
  if (textActive) {
    if ((millis() - lastText_ms) > TEXT_DISPLAY_MS) {
      textActive = false;
      lastText = "";
      display.clearDisplay();
      eyes.update(); // redraw face without text
    }
  }
}

void draw_text_overlay(){
  if (!textActive) return;
  display.fillRect(0, TEXT_AREA_Y, SCREEN_WIDTH, SCREEN_HEIGHT - TEXT_AREA_Y, SSD1306_BLACK);
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, TEXT_AREA_Y);
  display.println(lastText);
  display.display();
}

void setup() {
  Serial.begin(115200);
  randomSeed(esp_random());
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(200); Serial.print("."); }
  Serial.println("\nWiFi OK");

  i2s_init_fulldup();
  ws.onMessage(onMessage);
  ws.onEvent([](WebsocketsEvent ev, String){ if (ev==WebsocketsEvent::ConnectionOpened) Serial.println("[WS] opened"); });
  ws.connect(WS_URL);

  start_oled();
}

void change_face(int& face_count){
  face_count++;
  if(face_count == FACE_INTERVAL_FRAMES){
    Serial.println("\nChange Face.");
    int r = random(4);
    switch(r){
      case 0:
        eyes.anim_laugh();
        break;
      case 1:
        eyes.anim_confused();
        break;
      case 2:
        eyes.setCuriosity(true);
        break;
      case 3:
        eyes.setIdleMode(true);
        break;
    }
    face_count = 0;
  }
}

void reset_face(){
  eyes.setPosition(DEFAULT); 
  eyes.setIdleMode(false);
  eyes.setCuriosity(false);
}

void loop() {
  // 20ms分のマイク読み取り（32bitフレーム）
  size_t bytes_read = 0;
  i2s_read(I2S_PORT, rx32_buf, sizeof(rx32_buf), &bytes_read, portMAX_DELAY);
  if (bytes_read == sizeof(rx32_buf)) {
    convert_24to16(rx32_buf, pcm16_buf, FRAME_SAMPLES);
    send_frame_base64((const uint8_t*)pcm16_buf, FRAME_BYTES_16);
  }
  ws.poll();
  change_face(count);
  eyes.update();
  draw_text_overlay();
  loop_oled_idle();
  if (is_speak_face && (millis() - last_tts_ms) > 5000) {
    is_speak_face = false;
    eyes.setMood(DEFAULT);
    Serial.println("\nChange Default Face.");
  }
}
