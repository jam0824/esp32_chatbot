// esp32_tts_player.ino
// Board: ESP32 Dev Module
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoWebsockets.h>
#include "driver/i2s.h"
#include "mbedtls/base64.h"
using namespace websockets;

const char* WIFI_SSID = "TP-Link_C4D5";
const char* WIFI_PASS = "";
const char* WS_URL    = "ws://192.168.1.151:8000/ws_tts";

static const int SAMPLE_RATE = 16000;
static const int FRAME_BYTES = 640; // 20ms

// MAX98357A (TX のみ)
#define I2S_NUM_TX     I2S_NUM_1
#define I2S_BCLK       27    // BCLK
#define I2S_LRC        26    // LRC (LRCLK)
#define I2S_DOUT       25    // DIN (amp への出力)

WebsocketsClient ws;

void i2s_init_tx() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = 0,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
    .use_apll = false,
    .tx_desc_auto_clear = true,
    .fixed_mclk = 0
  };
  i2s_pin_config_t pins = {
    .mck_io_num   = I2S_PIN_NO_CHANGE,
    .bck_io_num   = I2S_BCLK,
    .ws_io_num    = I2S_LRC,
    .data_out_num = I2S_DOUT,
    .data_in_num  = I2S_PIN_NO_CHANGE
  };
  i2s_driver_install(I2S_NUM_TX, &cfg, 0, NULL);
  i2s_set_pin(I2S_NUM_TX, &pins);
  i2s_set_clk(I2S_NUM_TX, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
}

void onMessage(WebsocketsMessage msg) {
  if (msg.isText()) {
    // Base64 -> バイナリPCMに復元
    String b64 = msg.data();
    size_t out_len = (b64.length() * 3) / 4 + 8;
    std::unique_ptr<uint8_t[]> buf(new uint8_t[out_len]);
    size_t olen = 0;
    int rc = mbedtls_base64_decode(
      buf.get(), out_len, &olen,
      (const unsigned char*)b64.c_str(), b64.length()
    );
    if (rc == 0 && olen > 0) {
      size_t written = 0;
      i2s_write(I2S_NUM_TX, buf.get(), olen, &written, portMAX_DELAY);
    }
  }
  // バイナリは今回使わない
}

void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(200); Serial.print("."); }
  Serial.println("\nWiFi OK");

  i2s_init_tx();

  ws.onMessage(onMessage);
  ws.connect(WS_URL);
}

void loop() {
  ws.poll();
  delay(1);
}
