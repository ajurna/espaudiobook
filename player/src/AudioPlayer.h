#pragma once
#include "Arduino.h"

#define MAX_BOOKS 40
#define MAX_CHAPTERS 300
#define MAX_CACHED 200

// The roller shows books, or the chapters of the book that is open.
typedef enum {
    MODE_BOOKS = 0,
    MODE_CHAPTERS = 1
} PlayerMode;

struct Book {
    char id[16];
    char title[40];
    char author[34];
};

bool AudioPlayer_Init();

// Audio task (Core 1) and the cache task that fills the card while you listen.
void AudioPlayer_Task(void *parameter);
void Cache_Task(void *parameter);

void AudioPlayer_SetMode(PlayerMode mode);
PlayerMode AudioPlayer_GetMode();

bool AudioPlayer_Play();
void AudioPlayer_Stop();
bool AudioPlayer_TogglePlayPause();
bool AudioPlayer_Next();
bool AudioPlayer_Previous();

// Roller list: books or chapters, depending on the mode.
int AudioPlayer_GetListCount();
const char* AudioPlayer_GetListLabel(int index);
int AudioPlayer_GetSelectedIndex();
bool AudioPlayer_SetSelected(int index);

bool AudioPlayer_IsPlaying();
uint32_t AudioPlayer_GetTotalDuration();   // seconds of the chapter being played
uint32_t AudioPlayer_GetElapsedTime();
float AudioPlayer_GetProgress();
const char* AudioPlayer_GetCurrentName();  // book title

void AudioPlayer_SetVolume(uint8_t volume);
uint8_t AudioPlayer_GetVolume();
