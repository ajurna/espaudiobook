#include "AudioPlayer.h"
#include "Sidecar.h"
#include "config.h"

#ifdef HARDWARE_V2
#include "Audio_ES8311.h"
#else
#include "Audio_PCM5101.h"
#endif

#include "SD_Card.h"
#include "WiFiManager.h"
#include <ArduinoJson.h>

// Player state
static PlayerMode currentMode = MODE_BOOKS;
static bool isPlaying = false;
static uint8_t currentVolume = DEFAULT_VOLUME;
static TaskHandle_t audioTaskHandle = NULL;
static TaskHandle_t cacheTaskHandle = NULL;
static uint32_t lastStateSave = 0;

static Book books[MAX_BOOKS];
static int bookCount = 0;
static int selectedBook = -1;
static int selectedChapter = 0;
static uint32_t resumeSec = 0;   // where the book was left off, inside its chapter
static uint32_t seekSec = 0;     // where the chapter being played started

static uint32_t chapterStart[MAX_CHAPTERS];   // ms from the start of the book
static uint32_t chapterDur[MAX_CHAPTERS];
static char chapterLabel[MAX_CHAPTERS][32];
static char chapterAsset[MAX_CHAPTERS][40];
static int chapterCount = 0;

// Card cache: /cache/<bookId>/<chapter>.mp3, and cache/index.txt in insert order for the size cap.
static char cachedPath[MAX_CACHED][64];
static long cachedBytes[MAX_CACHED];
static int cachedCount = 0;
static long cacheTotal = 0;
static int wantBook = -1;
static int wantChapter = -1;

static void cardPath(char* out, int bookIndex, int chapter) {
    snprintf(out, 64, "%s/%s/%d.mp3", CACHE_ROOT, books[bookIndex].id, chapter);
}

static void loadCacheIndex() {
    File f = SD_MMC.open(CACHE_INDEX);
    if (!f) return;
    while (f.available() && cachedCount < MAX_CACHED) {
        String line = f.readStringUntil('\n');
        int sp = line.indexOf(' ');
        if (sp <= 0) continue;
        snprintf(cachedPath[cachedCount], 64, "%s", line.c_str());
        cachedBytes[cachedCount] = atol(line.c_str() + sp + 1);
        cacheTotal += cachedBytes[cachedCount];
        cachedCount++;
    }
    f.close();
}

static void writeCacheIndex() {
    File f = SD_MMC.open(CACHE_INDEX, FILE_WRITE);
    if (!f) return;
    for (int i = 0; i < cachedCount; i++) f.printf("%s %ld\n", cachedPath[i], cachedBytes[i]);
    f.close();
}

// Oldest first: the card is a rolling window, not a library.
static void cacheEvict() {
    while (cachedCount > 0 && cacheTotal > CACHE_MAX_KB * 1024L) {
        SD_MMC.remove(cachedPath[0]);
        cacheTotal -= cachedBytes[0];
        for (int i = 0; i + 1 < cachedCount; i++) {
            strcpy(cachedPath[i], cachedPath[i + 1]);
            cachedBytes[i] = cachedBytes[i + 1];
        }
        cachedCount--;
    }
}

static void cacheChapter(int bookIndex, int chapter) {
    if (bookIndex < 0 || bookIndex >= bookCount || chapter < 0) return;

    char path[64];
    cardPath(path, bookIndex, chapter);
    if (SD_MMC.exists(path)) return;

    char dir[64];
    snprintf(dir, sizeof(dir), "%s/%s", CACHE_ROOT, books[bookIndex].id);
    if (!SD_MMC.exists(dir)) SD_MMC.mkdir(dir);

    char api[128];
    snprintf(api, sizeof(api), "/track?book=%s&chapter=%d", books[bookIndex].id, chapter);
    long bytes = Sidecar_Download(api, path);
    if (bytes <= 0 || cachedCount >= MAX_CACHED) return;

    snprintf(cachedPath[cachedCount], 64, "%s", path);
    cachedBytes[cachedCount] = bytes;
    cacheTotal += bytes;
    cachedCount++;
    cacheEvict();
    writeCacheIndex();
}

// A proportional byte offset can land mid-frame, so re-anchor to a sync word, as the sidecar does.
static long framePos(const char* path, long approx) {
    if (approx <= 0) return -1;
    File f = SD_MMC.open(path);
    if (!f) return -1;
    f.seek((uint32_t)approx);
    uint8_t buf[4096];
    int n = f.read(buf, sizeof(buf));
    f.close();
    for (int i = 0; i + 1 < n; i++) {
        if (buf[i] == 0xFF && (buf[i + 1] & 0xE0) == 0xE0) return approx + i;
    }
    return approx;
}

static void SavePosition() {
    if (selectedBook < 0 || selectedChapter >= chapterCount) return;
    char body[128];
    snprintf(body, sizeof(body), "{\"book\":\"%s\",\"assetId\":\"%s\",\"positionMs\":%lu}",
             books[selectedBook].id, chapterAsset[selectedChapter],
             (unsigned long)(chapterStart[selectedChapter] + 1000 * AudioPlayer_GetElapsedTime()));
    Sidecar_Put("/state", body);
}

static bool playChapter(int bookIndex, int chapter, uint32_t offsetSec) {
    if (bookIndex < 0 || bookIndex >= bookCount || chapter < 0 || chapter >= chapterCount) return false;

    audio.stopSong();
    selectedBook = bookIndex;
    selectedChapter = chapter;
    seekSec = offsetSec;

    char path[64];
    cardPath(path, bookIndex, chapter);
    if (SD_IsAvailable() && SD_MMC.exists(path)) {
        if (!audio.connecttoFS(SD_MMC, path, framePos(path, (long)offsetSec * 1000 * BYTES_PER_MS))) return false;
    } else {
        char stream[256];
        Sidecar_StreamUrl(stream, sizeof(stream), books[bookIndex].id, chapter, offsetSec * 1000);
        if (!audio.connecttohost(stream)) return false;
    }

    isPlaying = true;
    lastStateSave = millis();
    if (chapter + 1 < chapterCount) {   // fill the card with what comes next while you listen
        wantBook = bookIndex;
        wantChapter = chapter + 1;
    }
    return true;
}

static int loadLibrary() {
    String body = Sidecar_Get("/library");
    if (!body.length()) return 0;

    JsonDocument doc;
    if (deserializeJson(doc, body)) return 0;

    int count = 0;
    for (JsonVariant b : doc.as<JsonArray>()) {
        if (count >= MAX_BOOKS) break;
        // shortcut: BookOrbit book ids are integers; as<int> also parses the string ids the cached routes return.
        snprintf(books[count].id, 16, "%d", b["id"].as<int>());
        snprintf(books[count].title, 40, "%s", b["title"].as<const char*>());
        snprintf(books[count].author, 34, "%s", b["authors"][0].as<const char*>());
        count++;
    }
    return count;
}

// Book detail plus the state the sidecar keeps on BookOrbit, so a book reopens where it was left.
static bool loadBook(int bookIndex) {
    if (bookIndex < 0 || bookIndex >= bookCount) return false;

    char path[64];
    snprintf(path, sizeof(path), "/books/%s", books[bookIndex].id);
    String body = Sidecar_Get(path);
    if (!body.length()) return false;

    JsonDocument doc;
    if (deserializeJson(doc, body)) return false;

    chapterCount = 0;
    for (JsonVariant c : doc["chapters"].as<JsonArray>()) {
        if (chapterCount >= MAX_CHAPTERS) break;
        chapterStart[chapterCount] = c["startMs"].as<uint32_t>();
        chapterDur[chapterCount] = c["durationMs"].as<uint32_t>();
        snprintf(chapterAsset[chapterCount], 40, "%s", c["assetId"].as<const char*>());
        snprintf(chapterLabel[chapterCount], 32, "%d %s", chapterCount + 1, c["title"].as<const char*>());
        chapterCount++;
    }
    if (chapterCount == 0) return false;

    selectedChapter = 0;
    resumeSec = 0;
    snprintf(path, sizeof(path), "/books/%s/state", books[bookIndex].id);
    String state = Sidecar_Get(path);
    if (state.length()) {
        JsonDocument sdoc;
        if (!deserializeJson(sdoc, state)) {
            uint32_t posMs = sdoc["positionMs"].as<uint32_t>();
            for (int i = 0; i < chapterCount; i++) {
                if (posMs >= chapterStart[i] && posMs < chapterStart[i] + chapterDur[i]) {
                    selectedChapter = i;
                    resumeSec = (posMs - chapterStart[i]) / 1000;
                    break;
                }
            }
        }
    }
    return true;
}

// Initialize the audio player
bool AudioPlayer_Init() {
    if (!audio.setPinout(I2S_BCLK, I2S_LRC, I2S_DOUT)) {
        return false;
    }

    audio.setVolume(currentVolume);

    if (SD_IsAvailable()) {
        loadCacheIndex();
    }

    bookCount = loadLibrary();
    if (bookCount > 0) {
        loadBook(0);
    }

    // Dedicated audio task on Core 1
    BaseType_t taskCreated = xTaskCreatePinnedToCore(
        AudioPlayer_Task,
        "AudioTask",
        8192,
        NULL,
        3,
        &audioTaskHandle,
        1
    );

    if (taskCreated != pdPASS) {
        return false;
    }

    xTaskCreatePinnedToCore(Cache_Task, "CacheTask", 8192, NULL, 2, &cacheTaskHandle, 1);

    return true;
}

// Audio task to process audio data - runs on Core 1
void AudioPlayer_Task(void *parameter) {
    static bool wasPlaying = false;

    while (true) {
        if (audio.isRunning()) {
            audio.loop();
            wasPlaying = true;
            if (millis() - lastStateSave > SAVE_EVERY_SECONDS * 1000) {
                lastStateSave = millis();
                SavePosition();
            }
        } else if (wasPlaying && isPlaying) {
            SavePosition();
            wasPlaying = false;
            if (selectedChapter + 1 < chapterCount) {
                // Auto-advance to the next chapter
                playChapter(selectedBook, selectedChapter + 1, 0);
            } else {
                isPlaying = false;
            }
        }

        vTaskDelay(5); // Fast loop for smooth audio
    }
}

// Fills the card with the next chapter, in the background
void Cache_Task(void *parameter) {
    while (true) {
        if (bookCount == 0 && WiFi_IsConnected()) loadLibrary();   // wifi is up after boot, so retry the list
        if (wantChapter >= 0 && SD_IsAvailable()) {
            int book = wantBook;
            int chapter = wantChapter;
            wantChapter = -1;
            cacheChapter(book, chapter);
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// Set the current player mode
void AudioPlayer_SetMode(PlayerMode mode) {
    audio.stopSong();
    isPlaying = false;
    currentMode = mode;
}

// Get the current player mode
PlayerMode AudioPlayer_GetMode() {
    return currentMode;
}

// Play the selected book at the saved position, or the selected chapter
bool AudioPlayer_Play() {
    isPlaying = false;
    uint32_t offset = resumeSec;
    resumeSec = 0;   // from now on the device is at the position it is playing from
    return playChapter(selectedBook, selectedChapter, offset);
}

void AudioPlayer_Stop() {
    audio.stopSong();
    if (isPlaying) {
        SavePosition();
    }
    isPlaying = false;
}

bool AudioPlayer_TogglePlayPause() {
    if (isPlaying) {
        AudioPlayer_Stop();
    } else {
        isPlaying = AudioPlayer_Play();
    }
    return isPlaying;
}

bool AudioPlayer_Next() {
    return AudioPlayer_SetSelected(AudioPlayer_GetSelectedIndex() + 1);
}

bool AudioPlayer_Previous() {
    return AudioPlayer_SetSelected(AudioPlayer_GetSelectedIndex() - 1);
}

int AudioPlayer_GetListCount() {
    return (currentMode == MODE_BOOKS) ? bookCount : chapterCount;
}

const char* AudioPlayer_GetListLabel(int index) {
    if (currentMode == MODE_BOOKS) {
        return (index >= 0 && index < bookCount) ? books[index].title : "";
    }
    return (index >= 0 && index < chapterCount) ? chapterLabel[index] : "";
}

int AudioPlayer_GetSelectedIndex() {
    return (currentMode == MODE_BOOKS) ? selectedBook : selectedChapter;
}

bool AudioPlayer_SetSelected(int index) {
    if (currentMode == MODE_BOOKS) {
        if (index < 0 || index >= bookCount) return false;
        selectedBook = index;
        return loadBook(index);
    }
    if (index < 0 || index >= chapterCount) return false;
    selectedChapter = index;
    resumeSec = 0;   // picking a chapter from the list starts it at the beginning
    return true;
}

bool AudioPlayer_IsPlaying() {
    return isPlaying;
}

// Chapter seconds, not book seconds: the progress bar tracks one chapter at a time.
uint32_t AudioPlayer_GetTotalDuration() {
    if (selectedChapter >= chapterCount) return 0;
    return chapterDur[selectedChapter] / 1000;
}

uint32_t AudioPlayer_GetElapsedTime() {
    return seekSec + audio.getAudioCurrentTime();
}

float AudioPlayer_GetProgress() {
    uint32_t total = AudioPlayer_GetTotalDuration();
    uint32_t elapsed = AudioPlayer_GetElapsedTime();
    return (total > 0 && elapsed <= total) ? (float)elapsed / (float)total : 0.0f;
}

const char* AudioPlayer_GetCurrentName() {
    if (selectedBook < 0 || selectedBook >= bookCount) return "";
    return books[selectedBook].title;
}

void AudioPlayer_SetVolume(uint8_t volume) {
    if (volume > MAX_VOLUME) volume = MAX_VOLUME;
    currentVolume = volume;
    audio.setVolume(currentVolume);
}

uint8_t AudioPlayer_GetVolume() {
    return currentVolume;
}
