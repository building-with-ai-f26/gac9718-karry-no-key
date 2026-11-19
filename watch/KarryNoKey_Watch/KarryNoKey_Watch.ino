// Karry-No-Key watch, version 1.
// Press the BOOT button, sing for 12 seconds, and the watch sends the recording over Wi-Fi
// to the Karry-No-Key app on your laptop, which asks ACRCloud what the song is.
// The answer appears on the watch screen.
//
// Fill in secrets.h (Wi-Fi name, Wi-Fi password, laptop address) before uploading.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include "ESP_I2S.h"
#include "esp_check.h"
#include "Arduino_GFX_Library.h"
#include "pin_config.h"
#include "secrets.h"

// ---------- Settings ----------
const int RECORD_SECONDS = 12;
const int SAMPLE_RATE = 16000;                        // 16,000 sound samples a second: plenty for a voice
const uint8_t MIC_GAIN = 10;                          // microphone boost: 10 = 30 dB (range 0-14)
const int BOOT_BUTTON = 0;                            // the watch's BOOT button
const uint16_t BRAND_PINK = 0xEAD2;                   // #EE5A93
const uint16_t TEXT_WHITE = 0xFFFF;                   // white (defined here so library updates can't break it)

// ---------- Screen ----------
Arduino_DataBus *bus = new Arduino_ESP32QSPI(LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);
Arduino_GFX *gfx = new Arduino_CO5300(bus, LCD_RESET, 0, LCD_WIDTH, LCD_HEIGHT, 22, 0, 0, 0);

// ---------- Microphone ----------
I2SClass i2s;

// Draw one line of text centred across the screen. Built-in font: 6 x 8 pixels per character.
void centredLine(const String &text, int y, int size, uint16_t colour = TEXT_WHITE) {
  gfx->setTextSize(size);
  gfx->setTextColor(colour);
  int x = (gfx->width() - (int)text.length() * 6 * size) / 2;
  gfx->setCursor(x < 0 ? 0 : x, y);
  gfx->print(text);
}

// Draw text centred, breaking it onto several lines so it fits the screen. Returns the next free y.
int centredWrapped(const String &text, int y, int size, uint16_t colour = TEXT_WHITE) {
  int maxChars = (gfx->width() - 30) / (6 * size);
  String remaining = text;
  remaining.trim();
  while (remaining.length() > 0) {
    String line;
    if ((int)remaining.length() <= maxChars) {
      line = remaining;
      remaining = "";
    } else {
      int cut = remaining.lastIndexOf(' ', maxChars);
      if (cut <= 0) cut = maxChars;
      line = remaining.substring(0, cut);
      remaining = remaining.substring(cut);
      remaining.trim();
    }
    centredLine(line, y, size, colour);
    y += 8 * size + 8;
  }
  return y;
}

// A full screen: big heading, then smaller text underneath.
void showScreen(const String &heading, const String &body) {
  gfx->fillScreen(BRAND_PINK);
  int y = centredWrapped(heading, 150, 4);
  centredWrapped(body, y + 30, 2);
}

// ---------- Microphone chip ----------
// The watch's microphones are on an ES7210 chip. This switches it on, using the same
// start-up steps as Espressif's own ES7210 driver (esp_codec_dev), in "slave" mode:
// the watch's processor supplies the clock signals, the ES7210 sends back the sound.
const uint8_t ES7210_ADDRESS = 0x40;

bool es7210Write(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(ES7210_ADDRESS);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

uint8_t es7210Read(uint8_t reg) {
  Wire.beginTransmission(ES7210_ADDRESS);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom(ES7210_ADDRESS, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0;
}

// Change only some bits of a setting, leaving the rest as they are.
void es7210Update(uint8_t reg, uint8_t mask, uint8_t value) {
  es7210Write(reg, (es7210Read(reg) & ~mask) | (value & mask));
}

// Turn on microphones 1 and 2 at the chosen boost level.
void es7210SelectMics() {
  for (int i = 0; i < 4; i++) es7210Update(0x43 + i, 0x10, 0x00);
  es7210Write(0x4B, 0xFF);
  es7210Write(0x4C, 0xFF);
  es7210Update(0x01, 0x0B, 0x00);          // clocks on for mics 1 and 2
  es7210Write(0x4B, 0x00);                 // power on mics 1 and 2
  es7210Update(0x43, 0x10, 0x10);          // mic 1 on
  es7210Update(0x43, 0x0F, MIC_GAIN);
  es7210Update(0x44, 0x10, 0x10);          // mic 2 on
  es7210Update(0x44, 0x0F, MIC_GAIN);
  es7210Write(0x12, 0x00);                 // normal (not TDM) output
}

bool startAudioChip() {
  Wire.begin(IIC_SDA, IIC_SCL);
  Wire.beginTransmission(ES7210_ADDRESS);
  if (Wire.endTransmission() != 0) {
    Serial.println("ES7210 microphone chip not found");
    return false;
  }
  // Open: reset and basic setup
  es7210Write(0x00, 0xFF);
  es7210Write(0x00, 0x41);
  es7210Write(0x01, 0x3F);
  es7210Write(0x09, 0x30);
  es7210Write(0x0A, 0x30);
  es7210Write(0x23, 0x2A);
  es7210Write(0x22, 0x0A);
  es7210Write(0x20, 0x0A);
  es7210Write(0x21, 0x2A);
  es7210Update(0x08, 0x01, 0x00);          // slave mode
  es7210Write(0x40, 0x43);
  es7210Write(0x41, 0x70);                 // mic bias 2.87 V
  es7210Write(0x42, 0x70);
  es7210Write(0x07, 0x20);
  es7210Write(0x02, 0xC1);
  es7210SelectMics();
  // Sound format: 16-bit, standard I2S
  es7210Write(0x11, ((es7210Read(0x11) & 0x1F) | 0x60) & 0xFC);
  // Start
  uint8_t clockReg = es7210Read(0x01);
  es7210Write(0x01, clockReg);
  es7210Write(0x06, 0x00);
  es7210Write(0x40, 0x43);
  es7210Write(0x47, 0x08);
  es7210Write(0x48, 0x08);
  es7210Write(0x49, 0x08);
  es7210Write(0x4A, 0x08);
  es7210SelectMics();
  es7210Write(0x40, 0x43);
  es7210Write(0x00, 0x71);
  es7210Write(0x00, 0x41);
  Serial.println("ES7210 microphone chip started");
  return true;
}

// ---------- WAV file ----------
// Write the 44-byte header that turns raw samples into a standard .wav file.
void writeWavHeader(uint8_t *h, uint32_t dataBytes) {
  auto put32 = [&](int at, uint32_t v) { h[at] = v; h[at+1] = v >> 8; h[at+2] = v >> 16; h[at+3] = v >> 24; };
  auto put16 = [&](int at, uint16_t v) { h[at] = v; h[at+1] = v >> 8; };
  memcpy(h, "RIFF", 4);  put32(4, 36 + dataBytes);  memcpy(h + 8, "WAVE", 4);
  memcpy(h + 12, "fmt ", 4);  put32(16, 16);  put16(20, 1);  put16(22, 1);
  put32(24, SAMPLE_RATE);  put32(28, SAMPLE_RATE * 2);  put16(32, 2);  put16(34, 16);
  memcpy(h + 36, "data", 4);  put32(40, dataBytes);
}

// ---------- The main flow ----------
void waitForButton() {
  while (digitalRead(BOOT_BUTTON) == HIGH) delay(20);   // wait for a press
  while (digitalRead(BOOT_BUTTON) == LOW) delay(20);    // and the release
}

void singAndIdentify() {
  const uint32_t frames = (uint32_t)SAMPLE_RATE * RECORD_SECONDS;
  const uint32_t stereoBytes = frames * 2 * 2;          // 2 channels x 2 bytes each
  const uint32_t wavBytes = 44 + frames * 2;            // mono .wav file

  // The watch's extra memory (PSRAM) holds the recording.
  int16_t *stereo = (int16_t *)ps_malloc(stereoBytes);
  uint8_t *wav = (uint8_t *)ps_malloc(wavBytes);
  if (!stereo || !wav) {
    showScreen("Oops", "Not enough memory. Check PSRAM is set to OPI PSRAM in Tools.");
    free(stereo); free(wav);
    return;
  }

  // Record, counting down on screen once a second.
  const uint32_t bytesPerSecond = (uint32_t)SAMPLE_RATE * 4;
  uint32_t got = 0;
  for (int s = RECORD_SECONDS; s > 0; s--) {
    gfx->fillScreen(BRAND_PINK);
    centredLine("Sing!", 160, 6);
    centredLine(String(s), 260, 8);
    uint32_t target = got + bytesPerSecond;
    while (got < target) {
      size_t n = i2s.readBytes((char *)stereo + got, target - got);
      if (n == 0) break;
      got += n;
    }
  }
  const uint32_t framesGot = got / 4;

  // The mic may arrive on the left or right channel: keep whichever is louder.
  double energy[2] = {0, 0};
  for (uint32_t i = 0; i < framesGot; i++) {
    energy[0] += (double)stereo[2*i] * stereo[2*i];
    energy[1] += (double)stereo[2*i+1] * stereo[2*i+1];
  }
  int ch = energy[1] > energy[0] ? 1 : 0;
  int16_t *mono = (int16_t *)(wav + 44);
  for (uint32_t i = 0; i < framesGot; i++) mono[i] = stereo[2*i + ch];
  free(stereo);
  writeWavHeader(wav, framesGot * 2);

  // Too quiet? Don't bother ACRCloud.
  double loudness = framesGot ? sqrt(energy[ch] / framesGot) / 32768.0 : 0;
  Serial.printf("Recorded %u samples, loudness %.4f (channel %d)\n", framesGot, loudness, ch);
  if (loudness < 0.005) {
    showScreen("I couldn't hear you!", "Sing a bit louder, closer to the watch. Press BOOT to try again.");
    free(wav);
    return;
  }

  // Send it to the laptop app.
  showScreen("Listening...", "Asking ACRCloud what song that was");
  if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();
  HTTPClient http;
  String url = String("http://") + LAPTOP_IP + ":8000/identify-watch";
  http.begin(url);
  http.setTimeout(30000);
  http.addHeader("Content-Type", "audio/wav");
  int code = http.POST(wav, 44 + framesGot * 2);
  String reply = code > 0 ? http.getString() : "";
  http.end();
  free(wav);
  Serial.printf("Server replied %d: %s\n", code, reply.c_str());

  if (code != 200) {
    showScreen("Can't reach laptop", "Is the Karry-No-Key app running, on the same Wi-Fi, with the right address in secrets.h? Press BOOT to retry.");
    return;
  }

  // Reply lines: status, title, artist, message.
  String lines[4];
  int start = 0;
  for (int i = 0; i < 4; i++) {
    int end = reply.indexOf('\n', start);
    if (end < 0) end = reply.length();
    lines[i] = reply.substring(start, end);
    start = end + 1;
  }

  if (lines[0] == "match") {
    gfx->fillScreen(BRAND_PINK);
    centredLine("Is this...", 90, 3);
    int y = centredWrapped(lines[1], 160, 4);
    y = centredWrapped(lines[2], y + 20, 3);
    centredWrapped("Press BOOT to sing again", y + 50, 2);
  } else {
    String msg = lines[3].length() ? lines[3] : String("No match found.");
    showScreen("No match", msg + " Press BOOT to try again.");
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(BOOT_BUTTON, INPUT_PULLUP);
  gfx->begin();
  showScreen("Karry-No-Key", "Starting up...");

  // Microphone
  i2s.setPins(41, 45, 40, 42, 16);   // pins from Waveshare's audio example
  if (!i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)
      || !startAudioChip()) {
    showScreen("Mic problem", "The microphone didn't start. Try unplugging and replugging the watch.");
    return;
  }

  // Wi-Fi
  showScreen("Karry-No-Key", String("Joining Wi-Fi: ") + WIFI_NAME);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_NAME, WIFI_PASSWORD);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) delay(500);
  if (WiFi.status() != WL_CONNECTED) {
    showScreen("No Wi-Fi", String("Couldn't join ") + WIFI_NAME + ". Check the name and password in secrets.h, then upload again.");
    return;
  }
  Serial.print("Watch Wi-Fi address: ");
  Serial.println(WiFi.localIP());

  showScreen("Ready to sing!", "Press the BOOT button, then sing for 12 seconds");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) { delay(500); return; }
  waitForButton();
  singAndIdentify();
}
