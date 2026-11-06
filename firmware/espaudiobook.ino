/*
  espaudiobook.ino - audiobook player for the Waveshare ESP32-S3-Touch-LCD-1.85C (V2, ES8311)

  Talks only to the sidecar (see ../sidecar), which gives the device one stable API and
  always returns 48 kHz mono CBR MP3 - the ES8311 codec cannot run at 24 kHz and the device
  needs exactly one decoder path.

  Board pins (Waveshare V2): I2S MCK=2 BCK=48 LRCK=38 DOUT=47, codec I2C addr 0x18 (SDA 11/SCL 10),
  speaker amp enable = GPIO15 high, backlight = GPIO5, SD card (SDMMC 4-bit) CLK=14 CMD=17 D0=16.

  Needs: Arduino ESP32 core 2.x/3.x, LVGL 9.3, ESP32-audioI2S, ArduinoJson 7.
  The display/touch/I2C/codec files in this folder are Waveshare's Apache-2.0 examples, unmodified.
*/

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <SD_MMC.h>
#include <FS.h>
#include <Audio.h>
#include <vector>
#include <time.h>

#include "LVGL_Driver.h"     // pulls lv_conf.h, Display_ST77916.h, Touch_CST816.h, TCA9554PWR.h
#include "I2C_Driver.h"
#include "es8311.h"

#define WIFI_SSID  "your-ssid"
#define WIFI_PASS  "your-pass"
#define SIDECAR    "https://192.168.1.10:8090"   // nginx in front of the sidecar, reachable from the device
#define BOOK_USER  "your-username"
#define BOOK_PASS  "your-password"

// Paste the cert nginx serves for the sidecar (or the CA that signed it). The device owns the session
// and sends the Bearer token on every request; the sidecar only forwards it, so it holds no credential.
// shortcut: ESP32-audioI2S calls clientsecure.setInsecure() in its own begin(), so only the HTTPClient
// calls below verify this CA - the audio stream does not. Upgrade when the library can take a client.
#define CA_CERT R"(
-----BEGIN CERTIFICATE-----
paste certs/sidecar.crt here
-----END CERTIFICATE-----
)"

#define CACHE_MAX_KB 400000      // card cache cap, oldest chapter first
#define I2S_BCLK 48
#define I2S_LRC  38
#define I2S_DOUT 47
#define I2S_MCLK 2
#define PA_PIN   15
#define SD_CLK   14
#define SD_CMD   17
#define SD_D0    16

#define REPORT_EVERY_MS 15000
#define PREFETCH        3          // tracks to warm on the card while nothing is decoding

Audio audio;

struct Chapter { String assetId, title; uint32_t startMs; };

String bookId, bookTitle;
std::vector<Chapter> chapters;
int idx = -1;
bool playing = false, paused = false, finished = false, stalled = false;
uint8_t retry = 0;

lv_obj_t *booksPage, *playerPage, *statusLbl, *titleLbl, *chapLbl, *timeLbl, *bar;
uint32_t lastReport = 0, lastUi = 0;

void showPlayer();

/* ---------------- session (the device owns it, the sidecar only forwards it) ---------------- */

Preferences prefs;
String accessTok, lastRefresh;   // lastRefresh is filled by the login/refresh responses
long long tokUntil = 0;
bool jsonReq(const char* method, const String& path, JsonDocument& doc, const String& body, bool auth);

long long isoEpoch(const char* s) {
  int y, m, d, h, mi, sec;
  if (sscanf(s, "%d-%d-%dT%d:%d:%d", &y, &m, &d, &h, &mi, &sec) != 6) return 0;
  struct tm t = {};
  t.tm_year = y - 1900; t.tm_mon = m - 1; t.tm_day = d; t.tm_hour = h; t.tm_min = mi; t.tm_sec = sec;
  return timegm(&t);
}

String savedRefresh() { prefs.open("auth", true); String s = prefs.getString("rt", ""); prefs.close(); return s; }

void saveSession() {
  prefs.open("auth", false);
  prefs.putString("token", accessTok);
  prefs.putLong("until", tokUntil);
  prefs.putString("rt", lastRefresh);
  prefs.close();
}

String token(bool force = false) {
  // a reboot resets the clock, so only trust the expiry once NTP has set it
  if (!force && accessTok.length() && !(time(nullptr) > 1600000000 && time(nullptr) >= tokUntil)) return accessTok;
  JsonDocument doc;
  String rt = savedRefresh();
  if (rt.length() && jsonReq("POST", "/auth/refresh", doc, "{\"refreshToken\":\"" + rt + "\"}", false)) {
    accessTok = doc["accessToken"].as<String>();
    tokUntil = isoEpoch(doc["accessTokenExpiresAt"].as<const char*>());
    if (doc["refreshToken"].is<const char*>()) lastRefresh = doc["refreshToken"].as<String>();
  } else if (jsonReq("POST", "/auth/login", doc,
                      "{\"username\":\"" + String(BOOK_USER) + "\",\"password\":\"" + String(BOOK_PASS) +
                      "\",\"clientKind\":\"native\",\"deviceLabel\":\"espaudiobook\"}", false)) {
    accessTok = doc["accessToken"].as<String>();
    tokUntil = isoEpoch(doc["accessTokenExpiresAt"].as<const char*>());
    if (doc["refreshToken"].is<const char*>()) lastRefresh = doc["refreshToken"].as<String>();
  }
  saveSession();
  return accessTok;
}

bool jsonReq(const char* method, const String& path, JsonDocument& doc, const String& body = "", bool auth = true) {
  for (int attempt = 0; attempt < 2; attempt++) {
    String tok = auth ? token(attempt > 0) : accessTok;
    if (auth && !tok.length()) return false;
    HTTPClient http;
    http.setTimeout(8000);
    if (strncmp(SIDECAR, "https", 5) == 0) {
      WiFiClientSecure secure;
      secure.setCACert(CA_CERT);
      http.begin(secure, SIDECAR + path);
    } else {
      http.begin(SIDECAR + path);
    }
    http.addHeader("content-type", "application/json");
    if (tok.length()) http.setAuthorization(("Bearer " + tok).c_str());
    int code = body.length() ? (strcmp(method, "POST") == 0 ? http.POST(body) : http.PUT(body)) : http.GET();
    bool ok = (code == 200) && !deserializeJson(doc, http.getString());
    http.end();
    if (ok) return true;
    if (code == 401) accessTok = "";      // session revoked or password changed -> re-authenticate
    else return false;
  }
  return false;
}

bool getJson(const String& path, JsonDocument& doc) { return jsonReq("GET", path, doc, ""); }

/* ---------------- sidecar client ---------------- */

String cachePath(const String& assetId) { return "/sdcard/" + bookId + "/" + assetId + ".mp3"; }

size_t cachedSize(const String& assetId) {
  File f = SD_MMC.open(cachePath(assetId));
  if (!f) return 0;
  size_t s = f.size();
  f.close();
  return s;
}

// Oldest = earliest cached chapter in manifest order, i.e. the one already listened to. SDFS reports
// no free space or file times, so the total is summed from the manifest's chapters.
void evictCard(const String& keep) {
  size_t used = 0;
  for (const auto& c : chapters) used += cachedSize(c.assetId);
  if (used <= (size_t)CACHE_MAX_KB * 1024) return;
  for (const auto& c : chapters) {
    if (c.assetId == keep) continue;
    size_t s = cachedSize(c.assetId);
    if (!s) continue;
    SD_MMC.remove(cachePath(c.assetId).c_str());
    used -= s;
    if (used <= (size_t)CACHE_MAX_KB * 1024) break;
  }
}

// Only complete tracks count as a cache, otherwise the player would play half a chapter: the
// sidecar sends Content-Length only for tracks it has fully transcoded.
bool download(const String& assetId) {
  HTTPClient http;
  http.setTimeout(15000);
  if (strncmp(SIDECAR, "https", 5) == 0) {
    WiFiClientSecure secure;
    secure.setCACert(CA_CERT);
    http.begin(secure, SIDECAR + "/track/" + bookId + "/" + assetId);
  } else {
    http.begin(SIDECAR + "/track/" + bookId + "/" + assetId);
  }
  http.setAuthorization(("Bearer " + token()).c_str());
  evictCard(assetId);
  if (http.GET() != 200) { http.end(); return false; }
  size_t len = http.contentLength();
  if (!len) { http.end(); return false; }        // unknown length = a stream, not a cache entry
  String part = cachePath(assetId) + ".part";
  File f = SD_MMC.open(part, O_WRONLY);
  if (!f) { http.end(); return false; }
  WiFiClient* s = http.getStreamPtr();
  size_t got = 0;
  while (got < len && s && s->connected()) {
    while (s->available()) { f.write((uint8_t)s->read()); got++; }
  }
  f.close();
  http.end();
  if (got != len) { SD_MMC.remove(part); return false; }
  SD_MMC.rename(part, cachePath(assetId).c_str());
  return true;
}

uint32_t positionMs() { return idx < 0 ? 0 : chapters[idx].startMs + audio.getAudioCurrentTime() * 1000; }

void report() {
  if (idx < 0) return;
  JsonDocument doc;
  String body = "{\"assetId\":\"" + chapters[idx].assetId + "\",\"positionMs\":" + String(positionMs()) + "}";
  jsonReq("PUT", "/books/" + bookId + "/state", doc, body);
}

/* ---------------- playback ---------------- */

void playChapter(int i, uint32_t resumeMs) {
  if (i < 0 || i >= (int)chapters.size()) return;
  idx = i;
  bool fromCard = cachedSize(chapters[i].assetId) > 0;
  if (fromCard) {
    if (!audio.connecttoFS(SD_MMC, cachePath(chapters[i].assetId).c_str())) return;
    if (resumeMs) audio.setAudioPlayPosition(resumeMs / 1000);   // works because our output is CBR
  } else {
    // the audio library builds its own request line and cannot add an Authorization header
    // (it only sends Basic auth), so the token rides in the query for the stream path.
    String url = SIDECAR + "/track/" + bookId + "/" + chapters[i].assetId + "?token=" + token();
    if (resumeMs) url += "&from_ms=" + String(resumeMs);
    if (!audio.connecttohost(url.c_str())) return;
  }
  playing = true;
  paused = false;
  finished = false;
  lastReport = millis();
  showPlayer();
}

// The library's weak callbacks fire inside audio.loop(), so they only set a flag;
// the main loop does the reconnect.
void audio_eof_mp3(const char*)   { finished = true; }
void audio_eof_stream(const char*) { finished = true; }

/* ---------------- UI ---------------- */

void showPage(lv_obj_t* page) {
  lvgl_port_lock(0);
  lv_obj_add_flag(booksPage, LV_OBJ_FLAG_HIDDEN);
  lv_obj_add_flag(playerPage, LV_OBJ_FLAG_HIDDEN);
  lv_obj_clear_flag(page, LV_OBJ_FLAG_HIDDEN);
  lvgl_port_unlock();
}

void refreshUi() {
  if (idx < 0) return;
  lvgl_port_lock(0);
  uint32_t dur = audio.getAudioFileDuration(), now = audio.getAudioCurrentTime();
  lv_label_set_text(titleLbl, bookTitle.c_str());
  lv_label_set_text(chapLbl, chapters[idx].title.c_str());
  lv_label_set_text_fmt(timeLbl, "%d:%02d / %d:%02d", now / 60, now % 60, dur / 60, dur % 60);
  lv_bar_set_value(bar, dur ? (int)(now * 100 / dur) : 0, LV_ANIM_OFF);
  lvgl_port_unlock();
}

void showPlayer() { showPage(playerPage); refreshUi(); }

void openBook(const char* id) {
  bookId = id;
  JsonDocument doc;
  if (!getJson("/books/" + bookId, doc)) return;
  bookTitle = doc["title"].as<const char*>();
  chapters.clear();
  lv_label_set_text(statusLbl, "");
  for (JsonVariant c : doc["chapters"].as<JsonArray>()) {
    chapters.push_back({c["assetId"].as<String>(), c["title"].as<String>(), c["startMs"].as<uint32_t>()});
  }
  if (chapters.empty()) return;

  int start = 0;
  uint32_t resume = 0;
  if (getJson("/books/" + bookId + "/state", doc) && doc["assetId"].is<const char*>()) {
    for (int i = 0; i < (int)chapters.size(); i++) {
      if (chapters[i].assetId == doc["assetId"].as<const char*>()) { start = i; resume = doc["positionMs"].as<uint32_t>(); break; }
    }
  }
  playChapter(start, resume);
}

void onBook(lv_event_t* e) { openBook((const char*)lv_event_get_user_data(e)); }

void onButton(lv_event_t* e) {
  int what = (int)(intptr_t)lv_event_get_user_data(e);
  if (what == 0) { if (idx > 0) playChapter(idx - 1, 0); }          // prev
  else if (what == 1) { audio.pauseResume(); paused = !paused; }     // play/pause (also warms the cache)
  else if (what == 2) { playChapter(idx + 1, 0); }                   // next
  else showPage(booksPage);                                          // back
}

lv_obj_t* makeButton(lv_obj_t* parent, const char* text, int what) {
  lv_obj_t* b = lv_button_create(parent);
  lv_obj_set_size(b, 64, 64);
  lv_obj_t* l = lv_label_create(b);
  lv_label_set_text(l, text);
  lv_obj_center(l);
  lv_obj_add_event_cb(b, onButton, LV_EVENT_CLICKED, (void*)(intptr_t)what);
  return b;
}

void buildUi() {
  booksPage = lv_list_create(lv_scr_act());
  lv_obj_set_size(booksPage, 360, 360);
  statusLbl = lv_label_create(lv_scr_act());
  lv_obj_align(statusLbl, LV_ALIGN_TOP_MID, 0, 6);
  lv_obj_add_flag(statusLbl, LV_OBJ_FLAG_HIDDEN);

  playerPage = lv_obj_create(lv_scr_act());
  lv_obj_set_size(playerPage, 360, 360);
  lv_obj_remove_style_all(playerPage);
  titleLbl = lv_label_create(playerPage);
  chapLbl = lv_label_create(playerPage);
  timeLbl = lv_label_create(playerPage);
  bar = lv_bar_create(playerPage);
  lv_bar_set_range(bar, 0, 100);
  lv_obj_set_width(bar, 280);
  lv_obj_align(bar, LV_ALIGN_CENTER, 0, -40);
  lv_obj_align(titleLbl, LV_ALIGN_TOP_MID, 0, 40);
  lv_obj_align(chapLbl, LV_ALIGN_CENTER, 0, 0);
  lv_obj_align(timeLbl, LV_ALIGN_CENTER, 0, 20);
  lv_obj_t* row = lv_obj_create(playerPage);
  lv_obj_remove_style_all(row);
  lv_obj_set_size(row, 300, 64);
  lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_align(row, LV_ALIGN_BOTTOM_MID, 0, -40);
  makeButton(row, "prev", 0);
  makeButton(row, "play", 1);
  makeButton(row, "next", 2);
  makeButton(row, "back", 3);

  lv_obj_add_flag(playerPage, LV_OBJ_FLAG_HIDDEN);
}

void fillBookList() {
  JsonDocument doc;
  if (!getJson("/books", doc)) {
    lv_obj_clear_flag(statusLbl, LV_OBJ_FLAG_HIDDEN);
    lv_label_set_text(statusLbl, "sidecar unreachable");
    return;
  }
  for (JsonVariant b : doc.as<JsonArray>()) {
    String id = b["id"].as<String>();
    lv_obj_t* btn = lv_list_add_button(booksPage, NULL, b["title"].as<const char*>());
    lv_obj_add_event_cb(btn, onBook, LV_EVENT_CLICKED, strdup(id.c_str()));
  }
}

/* ---------------- setup ---------------- */

void initBoard() {
  I2C_Init();                 // SDA 11 / SCL 10, used by the ES8311 codec
  TCA9554PWR_Init(0x00);      // touch reset on EXIO1, LCD reset on EXIO2
  Touch_Init();
  Backlight_Init();
  LCD_Init();
  Lvgl_Init();                // runs LVGL in its own task; UI writes go through lvgl_port_lock

  es8311_handle_t codec = es8311_create(I2C_NUM_0, ES8311_ADDRRES_0);
  es8311_clock_config_t clk = {.mclk_inverted = false, .sclk_inverted = false, .mclk_from_mclk_pin = true,
                               .mclk_frequency = 48000 * 256, .sample_frequency = 48000};
  if (es8311_init(codec, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16)) Serial.println("codec failed");
  es8311_voice_volume_set(codec, 80, NULL);
  pinMode(PA_PIN, OUTPUT);
  digitalWrite(PA_PIN, HIGH);                 // NS4150B amp enable
}

void setup() {
  Serial.begin(115200);
  initBoard();

  pinMode(SD_D0, INPUT_PULLUP);        // as in Waveshare example 04
  SD_MMC.setPins(SD_CLK, SD_CMD, SD_D0);
  if (!SD_MMC.begin("/sdcard", true, false, 20000)) Serial.println("no SD card");

  audio.setPinout(I2S_BCLK, I2S_LRC, I2S_DOUT, I2S_MCLK);
  audio.setVolume(21);

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.printf("\nconnected, ip %s\n", WiFi.localIP().toString().c_str());

  configTzTime("UTC0", "pool.ntp.org");        // token expiry needs a real clock
  delay(3000);
  prefs.open("auth", true);
  accessTok = prefs.getString("token", "");
  tokUntil = prefs.getLong("until", 0);
  lastRefresh = prefs.getString("rt", "");
  prefs.close();
  if (!token()) Serial.println("no session - check BOOK_USER/BOOK_PASS");

  buildUi();
  fillBookList();
}

void loop() {
  audio.loop();

  if (playing && !paused && !audio.isRunning() && !finished) stalled = true;   // Wi-Fi drop, no EOF

  if (stalled) {                                  // retry the same chapter a couple of times
    stalled = false;
    if (retry++ < 2) playChapter(idx, positionMs());
    else { retry = 0; idx++; playChapter(idx, 0); }
  }

  if (finished) {                     // end of track: report, advance, then warm the cache
    finished = false;
    report();
    if (idx + 1 < (int)chapters.size()) playChapter(idx + 1, 0);
    else { playing = false; showPage(booksPage); }
  }

  if (playing && !paused && millis() - lastReport > REPORT_EVERY_MS) {
    lastReport = millis();
    report();
  }

  // shortcut: prefetch only runs while nothing is decoding (idle or paused) - a download in this
  // loop would starve the decoder; upgrade to a download task pinned to core 0 if you want it live.
  if (idx >= 0 && (!playing || paused)) {
    int warmed = 0;
    for (int i = idx + 1; i < (int)chapters.size() && warmed < PREFETCH; i++) {
      if (cachedSize(chapters[i].assetId) == 0 && !download(chapters[i].assetId)) break;
      warmed++;
    }
  }

  if (playing && !paused && millis() - lastUi > 1000) {
    lastUi = millis();
    refreshUi();
  }
}
