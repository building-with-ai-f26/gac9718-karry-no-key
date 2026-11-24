// Karry-No-Key watch, version 3.
// Tap "Tap to sing" (or press BOOT), sing for 12 seconds, and the watch sends the recording over
// Wi-Fi to the Karry-No-Key app (the live site, karry-no-key.onrender.com), which asks ACRCloud
// what the song is.
// The watch asks "Is this [song]?" with two touch buttons: "Add to playlist" saves it to your
// Spotify "Sung It" playlist; "Not my song" skips it and tells PostHog the guess was wrong.
//
// Fill in secrets.h (Wi-Fi name and password) before uploading.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#include <Wire.h>
#include "ESP_I2S.h"
#include "esp_check.h"
#include "Arduino_GFX_Library.h"
// Karry-No-Key's brand font (Fredoka), converted for the watch at the exact sizes used,
// so text stays crisp instead of being the tiny built-in font blown up.
#include "FredokaBold40.h"      // headings
#include "FredokaSemi28.h"      // buttons and song titles
#include "FredokaMedium20.h"    // smaller text
#include "FredokaDigits110.h"   // the big countdown numbers (digits only)
#include "pin_config.h"
#include "secrets.h"
#ifndef WATCH_KEY
#define WATCH_KEY ""   // no key yet: Find my stays switched off
#endif
// Where the Karry-No-Key app lives. The live site works on any Wi-Fi, with no laptop needed.
// To test with the app on your laptop instead, put this in secrets.h (with your laptop's address):
//   #define SERVER_URL "http://192.168.1.23:8000"
#ifndef SERVER_URL
#define SERVER_URL "https://karry-no-key.onrender.com"
#endif

// ---------- Settings ----------
const int SHORT_SING_SECONDS = 12;                   // the two lengths you can pick after "Tap to sing"
const int LONG_SING_SECONDS = 30;
const int SAMPLE_RATE = 16000;                        // 16,000 sound samples a second: plenty for a voice
const uint8_t MIC_GAIN = 10;                          // microphone boost: 10 = 30 dB (range 0-14)
const int BOOT_BUTTON = 0;                            // the watch's BOOT button
const uint16_t BRAND_PINK = 0xEAD2;                   // #EE5A93
const uint16_t TEXT_WHITE = 0xFFFF;                   // white (defined here so library updates can't break it)
const unsigned long CHECKIN_EVERY_MS = 20000;         // Find my: tell the app "I'm here" every 20 seconds

// Find my: a new 6-digit code each time the watch starts. It's shown on the home screen, and the
// Find my page asks for it, so only someone who has seen this watch can check on it or ring it.
String findCode;

// ---------- On-screen buttons (position and size in pixels; the screen is 410 x 502) ----------
// Defined up here, before any functions, so Arduino's automatic setup can see them.
struct Button { int x, y, w, h; };
const Button SING_BUTTON   = { 35, 150, 340, 150 };   // home screen: the main, biggest button
const Button PLAY_BUTTON   = { 55, 330, 300, 80 };
const Button SHORT_BUTTON  = { 20, 220, 180, 130 };   // "How long will you sing?" screen, side by side
const Button LONG_BUTTON   = { 210, 220, 180, 130 };
const Button RESTART_BUTTON = { 30, 400, 170, 64 };   // shown while you sing
const Button STOP_BUTTON    = { 210, 400, 170, 64 };
const Button ADD_BUTTON    = { 30, 330, 350, 70 };
const Button REJECT_BUTTON = { 30, 415, 350, 60 };

// ---------- Screen ----------
Arduino_DataBus *bus = new Arduino_ESP32QSPI(LCD_CS, LCD_SCLK, LCD_SDIO0, LCD_SDIO1, LCD_SDIO2, LCD_SDIO3);
Arduino_GFX *screen = new Arduino_CO5300(bus, LCD_RESET, 0, LCD_WIDTH, LCD_HEIGHT, 22, 0, 0, 0);
// This screen only accepts drawing in whole pairs of pixels, so smooth fonts and rounded corners
// (drawn one pixel at a time) don't show if sent directly. Instead we draw each screen into the
// watch's memory (a "canvas"), then send the finished picture in one go with gfx->flush().
Arduino_GFX *gfx = new Arduino_Canvas(LCD_WIDTH, LCD_HEIGHT, screen);

// ---------- Microphone ----------
I2SClass i2s;

// Width in pixels of some text in a given font.
int textWidth(const GFXfont *font, const String &text) {
  int w = 0;
  for (unsigned int i = 0; i < text.length(); i++) {
    uint8_t c = text[i];
    if (c >= font->first && c <= font->last) w += font->glyph[c - font->first].xAdvance;
  }
  return w;
}

// Draw one line of text centred across the screen. y is the baseline (the line letters sit on).
void centredLine(const String &text, int y, const GFXfont *font, uint16_t colour = TEXT_WHITE) {
  gfx->setFont(font);
  gfx->setTextSize(1);
  gfx->setTextColor(colour);
  int x = (gfx->width() - textWidth(font, text)) / 2;
  gfx->setCursor(x < 0 ? 0 : x, y);
  gfx->print(text);
}

// Draw text centred, breaking it onto several lines so it fits. Returns the next free baseline.
int centredWrapped(const String &text, int y, const GFXfont *font, uint16_t colour = TEXT_WHITE) {
  const int maxWidth = gfx->width() - 40;
  String remaining = text;
  remaining.trim();
  while (remaining.length() > 0) {
    // Take as many whole words as fit on this line.
    int cut = remaining.length();
    while (textWidth(font, remaining.substring(0, cut)) > maxWidth) {
      int space = remaining.lastIndexOf(' ', cut - 1);
      if (space <= 0) {                       // one very long word: cut it where it fits
        while (cut > 1 && textWidth(font, remaining.substring(0, cut)) > maxWidth) cut--;
        break;
      }
      cut = space;
    }
    centredLine(remaining.substring(0, cut), y, font, colour);
    remaining = remaining.substring(cut);
    remaining.trim();
    y += font->yAdvance;
  }
  return y;
}

// A full screen: heading, then smaller text underneath.
void showScreen(const String &heading, const String &body) {
  gfx->fillScreen(BRAND_PINK);
  int y = centredWrapped(heading, 190, &FredokaBold40);
  centredWrapped(body, y + 12, &FredokaMedium20);
  gfx->flush();
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

// ---------- Touchscreen ----------
// The touch chip (FT3168) sits on the same I2C wires as the microphone chip.
// Register 0x02 = how many fingers are touching; 0x03-0x06 = where the first finger is.
const uint8_t TOUCH_ADDRESS = 0x38;
bool touchReady = false;

bool startTouch() {
  // Reset the touch chip, the same way Waveshare's driver does.
  pinMode(TP_RESET, OUTPUT);
  digitalWrite(TP_RESET, HIGH);
  delay(1);
  digitalWrite(TP_RESET, LOW);
  delay(20);
  digitalWrite(TP_RESET, HIGH);
  delay(50);
  // The touch chip can take a moment to wake up, so try for up to 3 seconds.
  for (int attempt = 0; attempt < 15 && !touchReady; attempt++) {
    Wire.beginTransmission(TOUCH_ADDRESS);
    touchReady = (Wire.endTransmission() == 0);
    if (!touchReady) delay(200);
  }
  if (touchReady) {
    // Put it in "monitor" mode: it listens for touches (Waveshare's driver does the same).
    Wire.beginTransmission(TOUCH_ADDRESS);
    Wire.write(0xA5);
    Wire.write(0x01);
    Wire.endTransmission();
    delay(20);
  }
  Serial.println(touchReady ? "Touchscreen started" : "Touchscreen not found (BOOT button still works)");
  return touchReady;
}

// True while a finger is on the screen; fills in x and y (in screen pixels).
bool readTouch(int &x, int &y) {
  if (!touchReady) return false;
  Wire.beginTransmission(TOUCH_ADDRESS);
  Wire.write(0x02);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(TOUCH_ADDRESS, (uint8_t)5) != 5) return false;
  uint8_t fingers = Wire.read() & 0x0F;
  uint8_t xh = Wire.read(), xl = Wire.read(), yh = Wire.read(), yl = Wire.read();
  if (fingers == 0 || fingers > 2) return false;
  x = ((xh & 0x0F) << 8) | xl;
  y = ((yh & 0x0F) << 8) | yl;
  return true;
}

// ---------- Buttons on screen ----------

// filled = white button with pink writing; otherwise a white outline with white writing.
void drawButton(const Button &b, const String &label, bool filled, const GFXfont *font = &FredokaSemi28) {
  if (filled) gfx->fillRoundRect(b.x, b.y, b.w, b.h, b.h / 2, TEXT_WHITE);
  else {
    for (int i = 0; i < 3; i++)
      gfx->drawRoundRect(b.x + i, b.y + i, b.w - 2 * i, b.h - 2 * i, b.h / 2 - i, TEXT_WHITE);
  }
  int capHeight = font->glyph['H' - font->first].height;   // height of a capital letter
  gfx->setFont(font);
  gfx->setTextSize(1);
  gfx->setTextColor(filled ? BRAND_PINK : TEXT_WHITE);
  gfx->setCursor(b.x + (b.w - textWidth(font, label)) / 2, b.y + (b.h + capHeight) / 2);
  gfx->print(label);
}

// A little extra room around each button so it's easy to hit with a finger.
bool inside(const Button &b, int x, int y) {
  const int pad = 12;
  return x >= b.x - pad && x <= b.x + b.w + pad && y >= b.y - pad && y <= b.y + b.h + pad;
}

void waitForRelease() {
  int x, y;
  while (readTouch(x, y) || digitalRead(BOOT_BUTTON) == LOW) delay(20);
}

// Wait for a tap on one of the buttons (or BOOT, which counts as the first button).
// Returns 0 for the first button, 1 for the second, or -1 if nothing within timeoutMs (0 = wait forever).
int waitForChoice(const Button *buttons, int count, unsigned long timeoutMs) {
  waitForRelease();
  unsigned long start = millis();
  while (timeoutMs == 0 || millis() - start < timeoutMs) {
    if (digitalRead(BOOT_BUTTON) == LOW) { waitForRelease(); return 0; }
    int x, y;
    if (readTouch(x, y)) {
      Serial.printf("Touch at x=%d, y=%d\n", x, y);   // helps check taps line up with the buttons
      for (int i = 0; i < count; i++) {
        if (inside(buttons[i], x, y)) {
          gfx->fillRoundRect(buttons[i].x, buttons[i].y, buttons[i].w, buttons[i].h, buttons[i].h / 2, 0xF5B6);  // flash light pink
          gfx->flush();
          waitForRelease();
          return i;
        }
      }
    }
    delay(25);
  }
  return -1;
}

// Show a message for 5 seconds, then go back to the "Tap to sing" home screen.
void showThenReturn(const String &heading, const String &body) {
  showScreen(heading, body);
  delay(5000);
}

// ---------- Battery (the AXP2101 power chip, on the same I2C wires as the touchscreen) ----------
const uint8_t PMU_ADDRESS = 0x34;

int pmuRead(uint8_t reg) {
  Wire.beginTransmission(PMU_ADDRESS);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -1;
  Wire.requestFrom(PMU_ADDRESS, (uint8_t)1);
  return Wire.available() ? Wire.read() : -1;
}

void startBatteryGauge() {
  int ctrl = pmuRead(0x18);                 // bit 3 switches on the battery "fuel gauge"
  if (ctrl < 0) { Serial.println("Power chip not found: battery will show as unknown"); return; }
  Wire.beginTransmission(PMU_ADDRESS);
  Wire.write(0x18);
  Wire.write(ctrl | 0x08);
  Wire.endTransmission();
}

int batteryPercent() {                     // -1 = unknown
  int status = pmuRead(0x00);
  if (status < 0 || !(status & 0x08)) return -1;   // bit 3 = battery connected
  int pct = pmuRead(0xA4);
  return (pct >= 0 && pct <= 100) ? pct : -1;
}

bool isCharging() {
  int status2 = pmuRead(0x01);
  return status2 >= 0 && ((status2 >> 5) & 0x03) == 0x01;
}

// ---------- Talking to the Karry-No-Key app ----------
// The live site uses https (encrypted). The watch checks the site's certificate against the
// trusted list built into the ESP32, so it can't be tricked into talking to a fake server.
NetworkClientSecure secureClient;
NetworkClient plainClient;
const uint16_t WAKE_UP_MS = 65000;   // the free server sleeps when unused and takes up to a minute to wake

// Start a request to the app. Every request carries WATCH_KEY, so the app knows it's this watch.
bool openRequest(HTTPClient &http, const char *path, uint16_t timeoutMs) {
  String url = String(SERVER_URL) + path;
  bool ok = url.startsWith("https://") ? http.begin(secureClient, url) : http.begin(plainClient, url);
  http.setConnectTimeout(timeoutMs);
  http.setTimeout(timeoutMs);
  if (strlen(WATCH_KEY) > 0) http.addHeader("X-Watch-Key", WATCH_KEY);
  return ok;
}

// Send a small request to the app and return its reply ("" if it couldn't be reached).
String askApp(const char *path) {
  HTTPClient http;
  openRequest(http, path, WAKE_UP_MS);
  int code = http.POST((uint8_t *)"", 0);
  String reply = code == 200 ? http.getString() : "";
  http.end();
  Serial.printf("%s replied %d: %s\n", path, code, reply.c_str());
  return reply;
}

// Save the song the app just matched to the "Sung It" playlist.
void saveToSpotify(const String &title) {
  showScreen("Saving...", title);
  String reply = askApp("/save-watch");
  if (reply.startsWith("saved")) {
    showThenReturn("Saved!", title + " is in your Sung It playlist.");
  } else {
    int nl = reply.indexOf('\n');
    String why = nl >= 0 ? reply.substring(nl + 1) : String("Couldn't reach Karry-No-Key.");
    showThenReturn("Not saved", why);
  }
}

// ---------- Find my Karry-No-Key ----------
// Tell the app the watch is here (battery, Wi-Fi signal, code). Returns true if someone pressed
// "Make it ring" on the Find my page.
bool checkIn() {
  if (strlen(WATCH_KEY) == 0 || WiFi.status() != WL_CONNECTED) return false;
  int pct = batteryPercent();
  String body = "battery=" + String(pct) + "\ncharging=" + String(isCharging() ? 1 : 0) +
                "\nrssi=" + String(WiFi.RSSI()) + "\nwifi=" + WiFi.SSID() + "\ncode=" + findCode;
  HTTPClient http;
  openRequest(http, "/watch/checkin", 5000);   // short, so the home screen never feels stuck
  int code = http.POST(body);
  String reply = code == 200 ? http.getString() : "";
  http.end();
  Serial.printf("Check-in: battery %d%%, signal %d, reply %d %s\n", pct, WiFi.RSSI(), code, reply.c_str());
  return reply.startsWith("ring");
}

// "Make it ring": flash the screen for 15 seconds, or until it's tapped.
void ringUntilFound() {
  unsigned long start = millis();
  bool bright = true;
  while (millis() - start < 15000) {
    gfx->fillScreen(bright ? TEXT_WHITE : BRAND_PINK);
    centredLine("Here I am!", 220, &FredokaBold40, bright ? BRAND_PINK : TEXT_WHITE);
    centredLine("Tap to stop", 290, &FredokaMedium20, bright ? BRAND_PINK : TEXT_WHITE);
    gfx->flush();
    bright = !bright;
    for (int i = 0; i < 10; i++) {          // check for a tap ten times between flashes
      int x, y;
      if (readTouch(x, y) || digitalRead(BOOT_BUTTON) == LOW) { waitForRelease(); return; }
      delay(40);
    }
  }
}

// ---------- Finish the Lyric on the watch (tap the missing word) ----------
const int GAME_ROUNDS = 5;
const Button CHOICE_BUTTONS[3] = { { 40, 290, 330, 58 }, { 40, 356, 330, 58 }, { 40, 422, 330, 58 } };
const Button AGAIN_BUTTON = { 55, 300, 300, 80 };
const Button HOME_BUTTON  = { 55, 395, 300, 70 };

// Split a reply into lines. Returns how many it found (up to maxLines).
int splitLines(const String &text, String *lines, int maxLines) {
  int count = 0, start = 0;
  while (count < maxLines && start <= (int)text.length()) {
    int end = text.indexOf('\n', start);
    if (end < 0) end = text.length();
    lines[count++] = text.substring(start, end);
    start = end + 1;
  }
  return count;
}

// Ask the app for a round. Lines: status, title, artist, before, after, 3 choices, answer.
bool fetchRound(String *r) {
  HTTPClient http;
  openRequest(http, "/game/round-watch", WAKE_UP_MS);
  int code = http.GET();
  String reply = code == 200 ? http.getString() : "";
  http.end();
  Serial.printf("Game round replied %d\n", code);
  int n = splitLines(reply, r, 9);
  return n == 9 && r[0] == "round";
}

// The lyric line with a gap, in the biggest font that leaves room for the buttons.
void drawLyric(const String &before, const String &gap, const String &after) {
  String line = before + (before.length() ? " " : "") + gap + after;
  if (after.length() && after[0] != ',' && after[0] != '.' && after[0] != '!' && after[0] != '?')
    line = before + (before.length() ? " " : "") + gap + " " + after;
  int maxW = LCD_WIDTH - 40;
  const GFXfont *font = textWidth(&FredokaSemi28, line) <= 3 * maxW ? &FredokaSemi28 : &FredokaMedium20;
  centredWrapped(line, 155, font);
}

void playFinishTheLyric() {
  int score = 0;
  for (int round = 1; round <= GAME_ROUNDS; round++) {
    showScreen("Finish the Lyric", "Finding a song...");
    String r[9];
    if (!fetchRound(r)) {
      showThenReturn("Finish the Lyric", r[0] == "error" ? r[1] : String("Couldn't reach Karry-No-Key. Try again in a minute."));
      return;
    }
    // r: 0 status, 1 title, 2 artist, 3 before, 4 after, 5-7 choices, 8 answer
    gfx->fillScreen(BRAND_PINK);
    centredLine("Round " + String(round) + " of " + String(GAME_ROUNDS) + "   Score " + String(score), 55, &FredokaMedium20);
    centredWrapped(r[1], 95, &FredokaMedium20);
    drawLyric(r[3], "____", r[4]);
    for (int i = 0; i < 3; i++) drawButton(CHOICE_BUTTONS[i], r[5 + i], false);
    gfx->flush();

    int pick = waitForChoice(CHOICE_BUTTONS, 3, 60000);
    if (pick < 0) { showThenReturn("Finish the Lyric", "Game paused. Tap to play again whenever you like."); return; }
    bool right = r[5 + pick] == r[8];
    if (right) score++;

    // Show the answer: the right word filled in, and which button was right.
    gfx->fillScreen(BRAND_PINK);
    centredLine(right ? "Correct! +10" : "Not quite!", 75, &FredokaBold40);
    drawLyric(r[3], r[8], r[4]);
    for (int i = 0; i < 3; i++) if (r[5 + i] == r[8]) drawButton(CHOICE_BUTTONS[i], r[5 + i], true);
    gfx->flush();
    HTTPClient http;   // tell the app, for PostHog
    openRequest(http, "/watch/game-answer", 5000);
    http.POST(String("correct=") + (right ? "1" : "0"));
    http.end();
    delay(2000);
  }

  gfx->fillScreen(BRAND_PINK);
  centredLine("You scored", 85, &FredokaSemi28);
  centredLine(String(score), 205, &FredokaDigits110);        // this font only has digits
  centredLine("out of " + String(GAME_ROUNDS), 255, &FredokaSemi28);
  drawButton(AGAIN_BUTTON, "Play again", true);
  drawButton(HOME_BUTTON, "Home", false);
  gfx->flush();
  const Button endButtons[2] = { AGAIN_BUTTON, HOME_BUTTON };
  if (waitForChoice(endButtons, 2, 30000) == 0) playFinishTheLyric();
}

// ---------- The main flow ----------
void showReadyScreen() {
  gfx->fillScreen(BRAND_PINK);
  centredLine("Karry-No-Key", 105, &FredokaBold40);
  drawButton(SING_BUTTON, "Tap to sing", true, &FredokaBold40);
  drawButton(PLAY_BUTTON, "Tap to play (Beta)", false);
  if (strlen(WATCH_KEY) > 0)
    centredLine("Find my code: " + findCode.substring(0, 3) + " " + findCode.substring(3), 475, &FredokaMedium20);
  gfx->flush();
}

// Draw the countdown screen: the seconds left, with Restart and Stop buttons.
void showCountdown(int secondsLeft) {
  gfx->fillScreen(BRAND_PINK);
  centredLine("Sing!", 130, &FredokaBold40);
  centredLine(String(secondsLeft), 320, &FredokaDigits110);
  drawButton(RESTART_BUTTON, "Restart", false);
  drawButton(STOP_BUTTON, "Stop", false);
  gfx->flush();
}

// Was Restart or Stop tapped? 0 = Restart, 1 = Stop, -1 = neither. Waits for the finger to lift.
int countdownTap() {
  int x, y;
  if (!readTouch(x, y)) return -1;
  int hit = inside(RESTART_BUTTON, x, y) ? 0 : inside(STOP_BUTTON, x, y) ? 1 : -1;
  if (hit >= 0) waitForRelease();
  return hit;
}

// After "Tap to sing": pick 12 or 30 seconds. Returns the seconds, or 0 for Back.
int chooseSingLength() {
  gfx->fillScreen(BRAND_PINK);
  centredLine("How long will", 130, &FredokaBold40);
  centredLine("you sing?", 180, &FredokaBold40);
  drawButton(SHORT_BUTTON, String(SHORT_SING_SECONDS) + " sec", true, &FredokaBold40);
  drawButton(LONG_BUTTON, String(LONG_SING_SECONDS) + " sec", true, &FredokaBold40);
  gfx->flush();
  const Button lengthButtons[2] = { SHORT_BUTTON, LONG_BUTTON };
  int pick = waitForChoice(lengthButtons, 2, 30000);   // BOOT = 12 seconds; nothing for 30 s = home
  return pick == 0 ? SHORT_SING_SECONDS : pick == 1 ? LONG_SING_SECONDS : 0;   // 0 = back home
}

void singAndIdentify() {
  const int RECORD_SECONDS = chooseSingLength();
  if (RECORD_SECONDS == 0) return;   // nothing picked within 30 seconds: back home
  const uint32_t frames = (uint32_t)SAMPLE_RATE * RECORD_SECONDS;
  const uint32_t stereoBytes = frames * 2 * 2;          // 2 channels x 2 bytes each
  const uint32_t wavBytes = 44 + frames * 2;            // mono .wav file

  // The watch's extra memory (PSRAM) holds the recording.
  int16_t *stereo = (int16_t *)ps_malloc(stereoBytes);
  uint8_t *wav = (uint8_t *)ps_malloc(wavBytes);
  if (!stereo || !wav) {
    showThenReturn("Oops", "Not enough memory. Check PSRAM is set to OPI PSRAM in Tools.");
    free(stereo); free(wav);
    return;
  }

  // Record, counting down on screen once a second. Restart starts the countdown again;
  // Stop goes back to the home screen. Taps are checked every quarter of a second.
  const uint32_t bytesPerSecond = (uint32_t)SAMPLE_RATE * 4;
  const uint32_t bytesPerQuarter = bytesPerSecond / 4;
  uint32_t got = 0;
  bool restarted;
  do {
    restarted = false;
    got = 0;
    for (int s = RECORD_SECONDS; s > 0 && !restarted; s--) {
      showCountdown(s);
      for (int q = 0; q < 4; q++) {
        uint32_t target = got + bytesPerQuarter;
        while (got < target) {
          size_t n = i2s.readBytes((char *)stereo + got, target - got);
          if (n == 0) break;
          got += n;
        }
        int tap = countdownTap();
        if (tap == 1) {                       // Stop
          free(stereo); free(wav);
          Serial.println("Stopped singing");
          return;
        }
        if (tap == 0) { restarted = true; break; }   // Restart
      }
    }
    if (restarted) Serial.println("Restarted the countdown");
  } while (restarted);
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
    showThenReturn("I couldn't hear you!", "Sing a bit louder, closer to the watch, then try again.");
    free(wav);
    return;
  }

  // Send it to the Karry-No-Key app.
  showScreen("Listening...", "Asking ACRCloud what song that was");
  if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();
  HTTPClient http;
  openRequest(http, "/identify-watch", WAKE_UP_MS);
  http.addHeader("Content-Type", "audio/wav");
  http.addHeader("X-Sing-Seconds", String(RECORD_SECONDS));
  int code = http.POST(wav, 44 + framesGot * 2);
  String reply = code > 0 ? http.getString() : "";
  http.end();
  free(wav);
  Serial.printf("Server replied %d: %s\n", code, reply.c_str());

  if (code != 200) {
    showThenReturn("Can't reach Karry-No-Key", "Check the watch is on Wi-Fi, then try again in a minute.");
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
    centredLine("Is this", 85, &FredokaSemi28);
    String question = lines[1] + "?";
    // Big title if it fits on two lines, otherwise a smaller size so it stays above the buttons.
    const GFXfont *titleFont = textWidth(&FredokaBold40, question) <= 2 * (LCD_WIDTH - 40) ? &FredokaBold40 : &FredokaSemi28;
    int y = centredWrapped(question, 145, titleFont);
    centredWrapped(lines[2], y + 4, &FredokaMedium20);
    drawButton(ADD_BUTTON, "Add to playlist", true);
    drawButton(REJECT_BUTTON, "Not my song", false);
    gfx->flush();
    const Button choices[2] = { ADD_BUTTON, REJECT_BUTTON };
    int choice = waitForChoice(choices, 2, 30000);
    if (choice == 0) {
      saveToSpotify(lines[1]);
    } else if (choice == 1) {
      askApp("/reject-watch");   // tells PostHog it guessed wrong
      showThenReturn("Got it", "Not saved. Try singing the chorus again.");
    } else {
      showThenReturn("Skipped", "Not saved.");
    }
  } else {
    String msg = lines[3].length() ? lines[3] : String("No match found.");
    showThenReturn("No match", msg);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(BOOT_BUTTON, INPUT_PULLUP);
  if (!gfx->begin()) Serial.println("Screen memory couldn't be set up (check PSRAM: OPI PSRAM)");
  gfx->setTextWrap(false);   // we break lines ourselves, neatly between words
  showScreen("Karry-No-Key", "Starting up...");

  // Microphone
  i2s.setPins(41, 45, 40, 42, 16);   // pins from Waveshare's audio example
  if (!i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO, I2S_STD_SLOT_BOTH)
      || !startAudioChip()) {
    showScreen("Mic problem", "The microphone didn't start. Try unplugging and replugging the watch.");
    return;
  }
  startTouch();   // uses the same I2C wires, which startAudioChip() switched on
  startBatteryGauge();
  findCode = String(100000 + esp_random() % 900000);

  // Wi-Fi
  showScreen("Karry-No-Key", String("Joining Wi-Fi: ") + WIFI_NAME);
  Serial.printf("Joining Wi-Fi: %s\n", WIFI_NAME);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_NAME, WIFI_PASSWORD);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) delay(500);
  if (WiFi.status() != WL_CONNECTED) {
    // Show which networks the watch CAN see: if yours isn't listed, it's on 5 GHz or out of range;
    // if it's listed with a slightly different name, copy that name into secrets.h exactly.
    Serial.printf("Couldn't join (Wi-Fi status %d). Networks the watch can see:\n", WiFi.status());
    WiFi.disconnect();
    int found = WiFi.scanNetworks();
    String seen;
    for (int i = 0; i < found && i < 8; i++) {
      Serial.printf("  \"%s\"  signal %d  channel %d\n", WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i));
      if (i < 4) seen += (seen.length() ? ", " : "") + WiFi.SSID(i);
    }
    if (found <= 0) Serial.println("  (none)");
    showScreen("No Wi-Fi", String("Couldn't join ") + WIFI_NAME + ". I can see: " + (seen.length() ? seen : String("nothing")));
    return;
  }
  Serial.println("Wi-Fi joined");
  Serial.print("Watch Wi-Fi address: ");
  Serial.println(WiFi.localIP());

  // Wake the app up now (it sleeps when nobody's using it), so the first sing is quick.
  secureClient.useBuiltinCACertBundle();
  showScreen("Karry-No-Key", "Waking up Karry-No-Key... this can take up to a minute");
  HTTPClient wake;
  openRequest(wake, "/", WAKE_UP_MS);
  int woke = wake.GET();
  wake.end();
  Serial.printf("Wake-up reply: %d\n", woke);
  checkIn();      // let Find my know straight away
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) { delay(500); return; }
  showReadyScreen();
  const Button homeButtons[2] = { SING_BUTTON, PLAY_BUTTON };
  // Wait for a tap (BOOT counts as "Tap to sing"). Every 20 seconds of waiting, check in with Find my.
  int choice;
  while ((choice = waitForChoice(homeButtons, 2, CHECKIN_EVERY_MS)) < 0) {
    if (checkIn()) {
      ringUntilFound();
      showReadyScreen();
    }
  }
  if (choice == 0) singAndIdentify();
  else playFinishTheLyric();
}
