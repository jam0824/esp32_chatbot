// esp32_mic_streamer.ino
// Board: ESP32 Dev Module
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoWebsockets.h>
#include "driver/i2s.h"
#include "mbedtls/base64.h"
using namespace websockets;

// ===== WiFi / WS =====
const char* WIFI_SSID = "TP-Link_C4D5";
const char* WIFI_PASS = "";
const char* WS_URL    = "ws://192.168.1.151:8000/ws_stt";

// ===== Audio =====
static const int SAMPLE_RATE = 16000;
static const int FRAME_SAMPLES = SAMPLE_RATE / 50;   // 20ms -> 320 samples
static const int FRAME_BYTES_16 = FRAME_SAMPLES * 2; // 640 bytes

// INMP441 (RX)
#define I2S_NUM_RX     I2S_NUM_0
#define I2S_BCLK       27  // SCK
#define I2S_LRC        26  // WS
#define I2S_DIN        34  // SD (in)

// Work buffers
static uint8_t rx32_buf[FRAME_SAMPLES * 4];   // 32bit * 320 = 1280B
static int16_t pcm16_buf[FRAME_SAMPLES];      // 16bit * 320 = 640B
WebsocketsClient ws;

void i2s_init_rx() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,     // INMP441は24bit左詰め → 32bitで受ける
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,      // L/RをGNDでLeft固定
    .communication_format = I2S_COMM_FORMAT_I2S,  // ← STAND_I2S から I2S に
    .intr_alloc_flags = 0,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };
  i2s_pin_config_t pins = {
    .mck_io_num   = I2S_PIN_NO_CHANGE,
    .bck_io_num   = I2S_BCLK,
    .ws_io_num    = I2S_LRC,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num  = I2S_DIN
  };
  i2s_driver_install(I2S_NUM_RX, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_RX, &pins);
  i2s_set_clk(I2S_NUM_RX, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_MONO);
}

// 32bit(24bit左詰) → 16bit 変換（上位24→16: 8ビット右シフト）
static inline void convert_24to16(const uint8_t* in32, int16_t* out16, size_t samples) {
  // INMP441はMSB側に24bitが乗る（I2Sの32bitスロット）。LE表現で [b3 b2 b1 b0] の b3がMSB。
  // 32bit値をint32_tとして読み、8bit右シフトして16bitへ丸める。
  const int32_t* p32 = reinterpret_cast<const int32_t*>(in32);
  for (size_t i = 0; i < samples; ++i) {
    int32_t v = p32[i];           // 32bit LE
    v >>= 8;                      // 24bit→16bit
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    out16[i] = (int16_t)v;
  }
}

void send_frame_base64(const uint8_t* pcm16, size_t len) {
  // Base64 エンコードしてテキスト送信
  size_t out_len = ((len + 2) / 3) * 4 + 8;
  std::unique_ptr<unsigned char[]> b64(new unsigned char[out_len]);
  size_t olen = 0;
  int rc = mbedtls_base64_encode(b64.get(), out_len, &olen, pcm16, len);
  if (rc == 0 && olen > 0) {
    ws.send(String((const char*)b64.get(), olen));
  }
}

void setup() {
  Serial.begin(115200);

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(200); Serial.print("."); }
  Serial.println("\nWiFi OK");

  i2s_init_rx();

  ws.onEvent([](WebsocketsEvent ev, String data){
    if (ev == WebsocketsEvent::ConnectionOpened)  Serial.println("[WS] opened");
    if (ev == WebsocketsEvent::ConnectionClosed)  Serial.println("[WS] closed");
  });
  ws.connect(WS_URL);
}

void loop() {
  // 20msぶんの32bitサンプルを読む（320サンプル→1280B）
  size_t bytes_read = 0;
  i2s_read(I2S_NUM_RX, rx32_buf, sizeof(rx32_buf), &bytes_read, portMAX_DELAY);
  if (bytes_read != sizeof(rx32_buf)) {
    // 足りないときはスキップ（通常は揃う）
    return;
  }

  // 32bit → 16bitへ詰め替え（640B）
  convert_24to16(rx32_buf, pcm16_buf, FRAME_SAMPLES);

  // 送出
  send_frame_base64((const uint8_t*)pcm16_buf, FRAME_BYTES_16);

  ws.poll();
  // delay(0); // 不要だが念のため
}
