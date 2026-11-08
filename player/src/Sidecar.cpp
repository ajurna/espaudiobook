#include "Sidecar.h"
#include "config.h"
#include "WiFiManager.h"
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SD_Card.h>

static String token;

static String url(const char* path) {
    return String("http://") + String(SIDECAR_HOST) + ":" + String(SIDECAR_PORT) + path;
}

static void begin(HTTPClient& http, const char* path) {
    // shortcut: login once at the first request; a token that expires is not refreshed, upgrade to
    // /auth/refresh if BookOrbit sessions ever outlive a battery charge.
    if (!token.length()) Sidecar_Login();
    http.begin(url(path));
    http.setTimeout(5000);
    if (token.length()) http.addHeader("Authorization", "Bearer " + token);
}

// The device password only lives here; the token is what the rest of the code uses.
bool Sidecar_Login() {
    if (!WiFi_IsConnected()) return false;
    HTTPClient http;
    http.begin(url("/auth/login"));
    http.setTimeout(8000);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(String("{\"username\":\"") + String(BOOKORBIT_USER) + "\",\"password\":\"" + String(BOOKORBIT_PASS) + "\"}");
    if (code != 200) {
        Serial.printf("sidecar login failed: %d\n", code);
        http.end();
        return false;
    }
    JsonDocument doc;
    if (deserializeJson(doc, http.getString())) {
        http.end();
        return false;
    }
    token = doc["accessToken"].as<String>();   // BookOrbit's own login response
    http.end();
    Serial.printf("sidecar token: %d chars\n", token.length());
    return token.length() > 0;
}

String Sidecar_Get(const char* path) {
    if (!WiFi_IsConnected() || !token.length()) return "";
    HTTPClient http;
    begin(http, path);
    int code = http.GET();
    if (code != 200) {
        Serial.printf("sidecar GET %s: %d\n", path, code);
        http.end();
        return "";
    }
    String body = http.getString();
    http.end();
    return body;
}

String Sidecar_Put(const char* path, const char* body) {
    if (!WiFi_IsConnected() || !token.length()) return "";
    HTTPClient http;
    begin(http, path);
    int code = http.PUT(String(body));
    if (code != 200) {
        Serial.printf("sidecar PUT %s: %d\n", path, code);
        http.end();
        return "";
    }
    String reply = http.getString();
    http.end();
    return reply;
}

// Save a whole chapter to the card. Returns the bytes written, 0 on failure.
long Sidecar_Download(const char* path, const char* saveTo) {
    if (!WiFi_IsConnected() || !token.length() || !SD_IsAvailable()) return 0;
    File f = SD_MMC.open(saveTo, FILE_WRITE);
    if (!f) return 0;

    HTTPClient http;
    begin(http, path);
    http.setTimeout(10000);
    int code = http.GET();
    long written = (code == 200) ? http.writeToStream(&f) : 0;
    http.end();
    f.close();

    if (written <= 0) {
        SD_MMC.remove(saveTo);
        Serial.printf("sidecar download %s: %d\n", path, code);
        return 0;
    }
    Serial.printf("cached %s (%ld bytes)\n", saveTo, written);
    return written;
}

// Token in the query string, not a header, so the audio library can use this URL as-is.
void Sidecar_StreamUrl(char* out, int n, const char* bookId, int chapter, int fromMs) {
    snprintf(out, n, "http://%s:%d/track?book=%s&chapter=%d&from_ms=%d&token=%s",
             SIDECAR_HOST, SIDECAR_PORT, bookId, chapter, fromMs, token.c_str());
}
