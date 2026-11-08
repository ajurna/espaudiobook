#include "UIController.h"
#include "config.h"

#ifdef HARDWARE_V2
#include "Audio_ES8311.h"
#else
#include "Audio_PCM5101.h"
#endif

#include "Display_ST77916.h"
#include "AudioPlayer.h"
#include "screens.h"
#include "images.h"
#include "WiFiManager.h"
#include "BAT_Driver.h"
#include <time.h>
#include <sys/time.h> 

// Maximum number of files and stations to display
const int MAX_FILES = 20;     // Increased to 10


// Timer for UI updates
static lv_timer_t *ui_update_timer = NULL;

// Static buffer to hold display text
static char timeDisplayBuffer[24];

// Last update time for UI refresh optimization
static uint32_t lastProgressUpdate = 0;
static uint32_t lastTimeUpdate = 0;

// Brightness setting
static int currentBrightness = 40;

// Static buffer for list display - allocate statically to avoid stack issues
static char listBuffer[1536]; // Increased size but still reasonable

// Set when play fails (no wifi, no sidecar, bad chapter)
static bool playError = false;

// Error display duration
static uint32_t errorDisplayStartTime = 0;
const uint32_t ERROR_DISPLAY_DURATION = 3000; // 3 seconds

// Time/Date display
static char timeBuffer[16];
static char dateBuffer[24];
static uint32_t lastClockUpdate = 0;
static bool ntpSynced = false;

// WiFi status update
static uint32_t lastWifiStatusUpdate = 0;
static char wifiLabelBuffer[8];

// Battery status update
static uint32_t lastBatteryUpdate = 0;
static char batteryLabelBuffer[8];

// Playback info update
static uint32_t lastPlaybackInfoUpdate = 0;
static char playbackInfoBuffer[48];

// Forward declaration of timer callback
static void UIController_TimerCallback(lv_timer_t *timer);

// Forward declaration of time functions
static void InitTimeSync();
static void UpdateClockDisplay();

// Forward declaration of WiFi status function
static void UpdateWifiStatus();

// Forward declaration of battery status function
static void UpdateBatteryStatus();

// Forward declaration of playback info function
static void UpdatePlaybackInfo();

// Brightness/Volume control mode
typedef enum {
    CONTROL_MODE_NONE = 0,
    CONTROL_MODE_BRIGHTNESS,
    CONTROL_MODE_VOLUME
} ControlMode;

static ControlMode currentControlMode = CONTROL_MODE_NONE;
static char brivolLabelBuffer[20];
static bool arcBeingAdjusted = false;  // Prevents accidental closure while adjusting
static uint32_t arcAdjustEndTime = 0;  // Debounce time after arc adjustment

// Forward declarations for brightness/volume control
static void UI_BrightnessButtonCallback(lv_event_t *e);
static void UI_VolumeButtonCallback(lv_event_t *e);
static void UI_BriVolArcCallback(lv_event_t *e);
static void UI_BriVolArcPressedCallback(lv_event_t *e);
static void UI_BriVolLabelCallback(lv_event_t *e);
static void UpdateBriVolDisplay();

// Initialize the UI controller
void UIController_Init() {
    // Set initial volume using audio object directly
    AudioPlayer_SetVolume(10);

    // Set initial brightness
    Set_Backlight(currentBrightness);

    // Start on the book list
    AudioPlayer_SetMode(MODE_BOOKS);

    // Initialize the audio player
    if (AudioPlayer_Init()) {
        Serial.println("Audio player initialized successfully");
        UIController_UpdateList();
    } else {
        Serial.println("Audio player initialization failed");
    }

    // Set up event handlers for UI elements
    UIController_SetupEvents();

    // Create timer for UI updates (250ms interval for good balance)
    ui_update_timer = lv_timer_create(UIController_TimerCallback, 250, NULL);

    // Initial UI update
    UIController_UpdateUI();

    // Make sure playback is paused on startup
    AudioPlayer_Stop();
    lv_obj_clear_state(objects.button_playpause, LV_STATE_CHECKED);

    // The book list is the starting view
    lv_obj_add_state(objects.button_mode, LV_STATE_CHECKED);

    // Initialize volume and brightness arc controls (visual only)
    lv_arc_set_value(objects.arc_volume, AudioPlayer_GetVolume() * 100 / 21);
    lv_arc_set_value(objects.arc_brightness, currentBrightness);

    // Initialize the control arc and hide the container initially
    lv_arc_set_value(objects.arc_brivol_controlls, 50);
    lv_obj_clear_state(objects.container_brivol_controlls, LV_STATE_CHECKED);

    // Initialize NTP time sync
    InitTimeSync();

    // Initialize battery ADC
    BAT_Init();
}

// Timer callback for UI updates
static void UIController_TimerCallback(lv_timer_t *timer) {
    UIController_UpdateUI();
}

// Process UI events in a loop (called from main loop)
void UIController_Loop() {
    // Empty function - UI updates handled by timer
}

// Update UI elements with current state
void UIController_UpdateUI() {
    uint32_t currentMillis = millis();

    // Refresh the list when the number of books or chapters changes
    static int lastListCount = -1;
    if (AudioPlayer_GetListCount() != lastListCount) {
        lastListCount = AudioPlayer_GetListCount();
        UIController_UpdateList();
    }

    static int lastSelectedIndex = -1;
    int selectedIndex = AudioPlayer_GetSelectedIndex();
    if (selectedIndex != lastSelectedIndex) {
        lv_roller_set_selected(objects.roller_list, selectedIndex, LV_ANIM_OFF);
        lastSelectedIndex = selectedIndex;
    }
    // Update playback time display every 1 second
    if (currentMillis - lastTimeUpdate >= 1000) {
        UIController_UpdateTimeDisplay();
        lastTimeUpdate = currentMillis;
    }

    // Update clock display every 1 second
    if (currentMillis - lastClockUpdate >= 1000) {
        UpdateClockDisplay();
        lastClockUpdate = currentMillis;
    }

    // Update WiFi status every 3 seconds
    if (currentMillis - lastWifiStatusUpdate >= 3000) {
        UpdateWifiStatus();
        lastWifiStatusUpdate = currentMillis;
    }

    // Update battery status every 10 seconds (battery changes slowly)
    if (currentMillis - lastBatteryUpdate >= 10000) {
        UpdateBatteryStatus();
        lastBatteryUpdate = currentMillis;
    }

    // Update progress bar every 500ms
    if (currentMillis - lastProgressUpdate >= 500) {
        UIController_UpdateProgressBar();
        lastProgressUpdate = currentMillis;
    }

    // Update playback info every 2 seconds
    if (currentMillis - lastPlaybackInfoUpdate >= 2000) {
        UpdatePlaybackInfo();
        lastPlaybackInfoUpdate = currentMillis;
    }

    // Update play/pause button state only when changed
    static bool lastPlayingState = false;
    bool currentPlaying = AudioPlayer_IsPlaying();
    if (currentPlaying != lastPlayingState) {
        if (currentPlaying) {
            lv_obj_add_state(objects.button_playpause, LV_STATE_CHECKED);
        } else {
            lv_obj_clear_state(objects.button_playpause, LV_STATE_CHECKED);
        }
        lastPlayingState = currentPlaying;
    }

    // Update mode button state only when changed
    static PlayerMode lastMode = MODE_CHAPTERS;
    PlayerMode currentMode = AudioPlayer_GetMode();
    if (currentMode != lastMode) {
        if (currentMode == MODE_BOOKS) {
            lv_obj_add_state(objects.button_mode, LV_STATE_CHECKED);
        } else {
            lv_obj_add_state(objects.button_mode, LV_STATE_CHECKED);
        }
        lastMode = currentMode;
    }
}

// Set up event handlers for UI elements
void UIController_SetupEvents() {
    // Mode button
    lv_obj_add_event_cb(objects.button_mode, UI_ModeButtonCallback, LV_EVENT_VALUE_CHANGED, NULL);

    // Play/Pause button
    lv_obj_add_event_cb(objects.button_playpause, UI_PlayPauseButtonCallback, LV_EVENT_CLICKED, NULL);

    // Next button
    lv_obj_add_event_cb(objects.button_next, UI_NextButtonCallback, LV_EVENT_CLICKED, NULL);

    // Previous button
    lv_obj_add_event_cb(objects.button_previous, UI_PreviousButtonCallback, LV_EVENT_CLICKED, NULL);

    // List roller
    lv_obj_add_event_cb(objects.roller_list, UI_ListCallback, LV_EVENT_VALUE_CHANGED, NULL);

    // Brightness button (invisible trigger)
    lv_obj_add_event_cb(objects.button_brightness, UI_BrightnessButtonCallback, LV_EVENT_CLICKED, NULL);

    // Volume button (invisible trigger)
    lv_obj_add_event_cb(objects.button_volume, UI_VolumeButtonCallback, LV_EVENT_CLICKED, NULL);

    // Brightness/Volume control arc - track pressing to prevent accidental closure
    lv_obj_add_event_cb(objects.arc_brivol_controlls, UI_BriVolArcCallback, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_add_event_cb(objects.arc_brivol_controlls, UI_BriVolArcPressedCallback, LV_EVENT_PRESSED, NULL);
    lv_obj_add_event_cb(objects.arc_brivol_controlls, UI_BriVolArcPressedCallback, LV_EVENT_RELEASED, NULL);

    // Brightness/Volume label - tap to close (only way to close)
    lv_obj_add_flag(objects.label_brivol_controlls, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(objects.label_brivol_controlls, UI_BriVolLabelCallback, LV_EVENT_CLICKED, NULL);
}

// Update the time display - time left in the chapter
void UIController_UpdateTimeDisplay() {
    if (playError) {
        lv_label_set_text(objects.label_playbacktime, "Error !!!");
    } else if (!AudioPlayer_IsPlaying()) {
        lv_label_set_text(objects.label_playbacktime, "--:--");
    } else {
        uint32_t total = AudioPlayer_GetTotalDuration();
        uint32_t elapsed = AudioPlayer_GetElapsedTime();
        uint32_t remaining = (total > elapsed) ? (total - elapsed) : 0;
        snprintf(timeDisplayBuffer, sizeof(timeDisplayBuffer), "-%02d:%02d",
                remaining / 60, remaining % 60);
        lv_label_set_text(objects.label_playbacktime, timeDisplayBuffer);
    }
}
// Update the progress bar - progress through the chapter
void UIController_UpdateProgressBar() {
    int progress = AudioPlayer_IsPlaying() ? (int)(AudioPlayer_GetProgress() * 100) : 0;
    lv_bar_set_value(objects.bar_status, progress, LV_ANIM_ON);
}
// Update the book or chapter list
void UIController_UpdateList() {
    memset(listBuffer, 0, sizeof(listBuffer));

    int itemsToShow = AudioPlayer_GetListCount();
    if (itemsToShow > MAX_FILES) itemsToShow = MAX_FILES;

    if (itemsToShow == 0) {
        strcpy(listBuffer, "No books");
    } else {
        char tempName[32];
        for (int i = 0; i < itemsToShow; i++) {
            const char* label = AudioPlayer_GetListLabel(i);
            strncpy(tempName, label, sizeof(tempName) - 1);
            tempName[sizeof(tempName) - 1] = '\0';

            // The roller is 229 px wide, so long names are truncated
            if (strlen(tempName) > 20) {
                tempName[17] = '.';
                tempName[18] = '.';
                tempName[19] = '.';
                tempName[20] = '\0';
            }

            strcat(listBuffer, tempName);
            if (i < itemsToShow - 1) strcat(listBuffer, "\n");
        }
    }

    lv_roller_set_options(objects.roller_list, listBuffer, LV_ROLLER_MODE_NORMAL);

    int selected = AudioPlayer_GetSelectedIndex();
    lv_roller_set_selected(objects.roller_list, (selected < itemsToShow) ? selected : 0, LV_ANIM_OFF);
}
// UI event callbacks - FORCED AUTO-PLAY FOR NAVIGATION

void UI_ModeButtonCallback(lv_event_t *e) {
    lv_obj_t *obj = lv_event_get_target(e);
    bool checked = lv_obj_has_state(obj, LV_STATE_CHECKED);

    // Stop any current playback first
    AudioPlayer_Stop();
    lv_obj_clear_state(objects.button_playpause, LV_STATE_CHECKED);

    playError = false;

    // Checked = the book list, unchecked = the chapters of the book that is open
    AudioPlayer_SetMode(checked ? MODE_BOOKS : MODE_CHAPTERS);
    // Update the list
    UIController_UpdateList();
}

void UI_PlayPauseButtonCallback(lv_event_t *e) {
    // Toggle playback - only plays when explicitly pressed
    bool isPlaying = AudioPlayer_TogglePlayPause();
    
    playError = !isPlaying;   // nothing to play, or the sidecar was not reachable
    // Update button state
    lv_obj_add_state(objects.button_playpause, isPlaying ? LV_STATE_CHECKED : 0);
}

// WITH FORCED AUTO-PLAY: Next button definitely plays the next track
void UI_NextButtonCallback(lv_event_t *e) {
    // Next moves to the next book or chapter and plays it
    if (AudioPlayer_Next()) {
        lv_roller_set_selected(objects.roller_list, AudioPlayer_GetSelectedIndex(), LV_ANIM_OFF);
        bool success = AudioPlayer_Play();
        playError = !success;
        if (playError) UIController_UpdateTimeDisplay();
        lv_obj_add_state(objects.button_playpause, success ? LV_STATE_CHECKED : 0);
    }
}
// WITH FORCED AUTO-PLAY: Previous button definitely plays the previous track
void UI_PreviousButtonCallback(lv_event_t *e) {
    AudioPlayer_Stop();

    if (AudioPlayer_Previous()) {
        lv_roller_set_selected(objects.roller_list, AudioPlayer_GetSelectedIndex(), LV_ANIM_OFF);
        bool success = AudioPlayer_Play();
        playError = !success;
        if (playError) UIController_UpdateTimeDisplay();
        lv_obj_add_state(objects.button_playpause, success ? LV_STATE_CHECKED : 0);
    }
}
// List selection doesn't auto-play
void UI_ListCallback(lv_event_t *e) {
    // Get selected index
    int selectedIndex = lv_roller_get_selected(objects.roller_list);
    
    // Stop any current playback
    AudioPlayer_Stop();
    lv_obj_clear_state(objects.button_playpause, LV_STATE_CHECKED);
    
    // Selecting a book loads it, selecting a chapter moves inside the book that is open
    AudioPlayer_SetSelected(selectedIndex);
}
// ============== Brightness/Volume Control Functions ==============

// Brightness button callback - activates brightness control mode
static void UI_BrightnessButtonCallback(lv_event_t *e) {
    // Prevent closing while arc is being adjusted (with 300ms debounce)
    if (arcBeingAdjusted || (millis() - arcAdjustEndTime < 300)) {
        return;
    }

    // Toggle brightness control
    if (currentControlMode == CONTROL_MODE_BRIGHTNESS) {
        // Already in brightness mode - do NOT close here, only label closes it
        return;
    } else {
        // Switch to brightness mode
        currentControlMode = CONTROL_MODE_BRIGHTNESS;
        lv_obj_add_state(objects.container_brivol_controlls, LV_STATE_CHECKED);

        // Set arc to current brightness value
        lv_arc_set_value(objects.arc_brivol_controlls, currentBrightness);
        UpdateBriVolDisplay();
    }
}

// Volume button callback - activates volume control mode
static void UI_VolumeButtonCallback(lv_event_t *e) {
    // Prevent closing while arc is being adjusted (with 300ms debounce)
    if (arcBeingAdjusted || (millis() - arcAdjustEndTime < 300)) {
        return;
    }

    // Toggle volume control
    if (currentControlMode == CONTROL_MODE_VOLUME) {
        // Already in volume mode - do NOT close here, only label closes it
        return;
    } else {
        // Switch to volume mode
        currentControlMode = CONTROL_MODE_VOLUME;
        lv_obj_add_state(objects.container_brivol_controlls, LV_STATE_CHECKED);

        // Set arc to current volume value (convert 0-21 to 0-100)
        int volumePercent = AudioPlayer_GetVolume() * 100 / 21;
        lv_arc_set_value(objects.arc_brivol_controlls, volumePercent);
        UpdateBriVolDisplay();
    }
}

// Arc pressed/released callback - track adjustment state
static void UI_BriVolArcPressedCallback(lv_event_t *e) {
    lv_event_code_t code = lv_event_get_code(e);
    if (code == LV_EVENT_PRESSED) {
        arcBeingAdjusted = true;
    } else if (code == LV_EVENT_RELEASED) {
        arcBeingAdjusted = false;
        arcAdjustEndTime = millis();  // Start debounce timer
    }
}

// Control arc callback - adjusts brightness or volume based on current mode
static void UI_BriVolArcCallback(lv_event_t *e) {
    arcBeingAdjusted = true;  // Also set during value change

    int value = lv_arc_get_value(objects.arc_brivol_controlls);

    if (currentControlMode == CONTROL_MODE_BRIGHTNESS) {
        // Update brightness
        currentBrightness = value;
        if (currentBrightness < MIN_BRIGHTNESS) currentBrightness = MIN_BRIGHTNESS;
        Set_Backlight(currentBrightness);

        // Update visual brightness arc
        lv_arc_set_value(objects.arc_brightness, currentBrightness);

    } else if (currentControlMode == CONTROL_MODE_VOLUME) {
        // Update volume (convert 0-100 to 0-21)
        uint8_t volume = (value * 21) / 100;
        AudioPlayer_SetVolume(volume);

        // Update visual volume arc
        lv_arc_set_value(objects.arc_volume, value);
    }

    // Update the label
    UpdateBriVolDisplay();
}

// Update the brightness/volume label display
static void UpdateBriVolDisplay() {
    int value = lv_arc_get_value(objects.arc_brivol_controlls);

    if (currentControlMode == CONTROL_MODE_BRIGHTNESS) {
        snprintf(brivolLabelBuffer, sizeof(brivolLabelBuffer), "Brightness\n%d%%", value);
    } else if (currentControlMode == CONTROL_MODE_VOLUME) {
        snprintf(brivolLabelBuffer, sizeof(brivolLabelBuffer), "Volume\n%d%%", value);
    }

    lv_label_set_text(objects.label_brivol_controlls, brivolLabelBuffer);
}

// Label click callback - hides the control container
static void UI_BriVolLabelCallback(lv_event_t *e) {
    currentControlMode = CONTROL_MODE_NONE;
    lv_obj_clear_state(objects.container_brivol_controlls, LV_STATE_CHECKED);
}

// ============== Time/Date Functions ==============

static void InitTimeSync() {
    // Configure NTP with timezone (more reliable than setenv/tzset)
    configTzTime(TIMEZONE_POSIX, NTP_SERVER1, NTP_SERVER2);

    Serial.println("NTP time sync initialized with timezone: " TIMEZONE_POSIX);
}

static void UpdateClockDisplay() {
    struct tm timeinfo;
    // Use very short timeout to avoid blocking - NTP syncs in background
    if (!getLocalTime(&timeinfo, 10)) {
        // NTP not synced yet
        if (!ntpSynced) {
            lv_label_set_text(objects.label_time, "--:--");
            lv_label_set_text(objects.label_date, "Syncing...");
        }
        return;
    }

    ntpSynced = true;

    // Format time as HH:MM in 12-hour format
    int hour = timeinfo.tm_hour;
    const char* ampm = (hour >= 12) ? "PM" : "AM";
    if (hour == 0) hour = 12;
    else if (hour > 12) hour -= 12;

    snprintf(timeBuffer, sizeof(timeBuffer), "%d:%02d", hour, timeinfo.tm_min);
    lv_label_set_text(objects.label_time, timeBuffer);

    // Format date as "DD DAY - MON YY" (e.g., "30 FRI - JAN 26")
    const char* dayNames[] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
    const char* monthNames[] = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                 "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};

    snprintf(dateBuffer, sizeof(dateBuffer), "%02d %s - %s %02d",
             timeinfo.tm_mday,
             dayNames[timeinfo.tm_wday],
             monthNames[timeinfo.tm_mon],
             timeinfo.tm_year % 100);

    lv_label_set_text(objects.label_date, dateBuffer);
}

// ============== WiFi Status Functions ==============

static void UpdateWifiStatus() {
    if (!WiFi_IsConnected()) {
        // WiFi disconnected - show wifi-00 image
        lv_img_set_src(objects.image_wifi, &img_wifi_00);
        lv_label_set_text(objects.label_wifi, "--");
    } else {
        // WiFi connected - check signal strength
        int rssi = WiFi_GetRSSI();
        // RSSI: -50 dBm = excellent, -60 = good, -70 = fair, -80 = weak
        // Convert to percentage: 100% at -50, 0% at -100
        int signalPercent = 2 * (rssi + 100);
        if (signalPercent > 100) signalPercent = 100;
        if (signalPercent < 0) signalPercent = 0;

        // Select WiFi image based on signal strength (wifi-01 to wifi-05)
        if (signalPercent >= 80) {
            lv_img_set_src(objects.image_wifi, &img_wifi_05);
        } else if (signalPercent >= 60) {
            lv_img_set_src(objects.image_wifi, &img_wifi_04);
        } else if (signalPercent >= 40) {
            lv_img_set_src(objects.image_wifi, &img_wifi_03);
        } else if (signalPercent >= 20) {
            lv_img_set_src(objects.image_wifi, &img_wifi_02);
        } else {
            lv_img_set_src(objects.image_wifi, &img_wifi_01);
        }

        // Update WiFi label with signal percentage
        snprintf(wifiLabelBuffer, sizeof(wifiLabelBuffer), "%d%%", signalPercent);
        lv_label_set_text(objects.label_wifi, wifiLabelBuffer);
    }
}

// ============== Battery Status Functions ==============

static void UpdateBatteryStatus() {
    float voltage = BAT_Get_Volts();

    // LiPo battery voltage range: 3.0V (empty) to 4.2V (full)
    // Map voltage to percentage
    int percent = (int)((voltage - 3.0f) / (4.2f - 3.0f) * 100.0f);

    // Clamp to 0-100 range
    if (percent > 100) percent = 100;
    if (percent < 0) percent = 0;

    // Update label with % sign
    snprintf(batteryLabelBuffer, sizeof(batteryLabelBuffer), "%d%%", percent);
    lv_label_set_text(objects.label_battery, batteryLabelBuffer);

    // Update battery bar
    lv_bar_set_value(objects.bar_battery, percent, LV_ANIM_ON);
}

// ============== Playback Info Functions ==============

static void UpdatePlaybackInfo() {
    snprintf(playbackInfoBuffer, sizeof(playbackInfoBuffer), "%s", AudioPlayer_GetCurrentName());
    lv_label_set_text(objects.label_playbackinfo, playbackInfoBuffer);
}