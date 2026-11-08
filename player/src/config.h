#pragma once

// WiFi
#define WIFI_SSID "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#define WIFI_TIMEOUT 10000

// Timezone
#define TIMEZONE_POSIX "TM-Zone"
#define NTP_SERVER1 "pool.ntp.org"
#define NTP_SERVER2 "time.nist.gov"

// Audio
#define DEFAULT_VOLUME 10
#define MAX_VOLUME 21

// Display
#define DEFAULT_BRIGHTNESS 40
#define MIN_BRIGHTNESS 10
#define MAX_BRIGHTNESS 100

// Sidecar (BookOrbit). The player only ever talks to the sidecar, not to BookOrbit.
#define SIDECAR_HOST "192.168.1.10"
#define SIDECAR_PORT 8090
#define BOOKORBIT_USER "your_username"
#define BOOKORBIT_PASS "your_password"

// Cached chapters: /cache/<bookId>/<chapter>.mp3 plus an insert-order manifest
// at /cache/index.txt, which is what gets pruned oldest-first.
#define CACHE_ROOT "/cache"
#define CACHE_INDEX "/cache/index.txt"
#define CACHE_MAX_KB 10000

// Playback position is saved to BookOrbit through the sidecar while playing.
#define SAVE_EVERY_SECONDS 30

// The sidecar delivers CBR 48 kbps mono, so 6 bytes per millisecond.
#define BYTES_PER_MS 6

// V2 Hardware
#ifdef HARDWARE_V2
    #define ES8311_I2C_ADDR     0x18
    #define ES8311_SAMPLE_RATE  44100
    #define ES8311_MCLK_MULTI   256
    #define ES7210_I2C_ADDR     0x40

    #define I2S_DOUT_PIN        47
    #define I2S_DIN_PIN         39
    #define I2S_BCLK_PIN        48
    #define I2S_LRC_PIN         38
    #define I2S_MCLK_PIN        2
    #define AMP_ENABLE_PIN      15
#endif
