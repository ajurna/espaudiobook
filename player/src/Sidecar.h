#pragma once
#include "Arduino.h"

// The only door to BookOrbit: the device has no BookOrbit client, everything goes through the sidecar.
bool Sidecar_Login();
String Sidecar_Get(const char* path);
String Sidecar_Put(const char* path, const char* body);

// Bytes on the card, because the audio library builds its own request line and cannot add a header.
long Sidecar_Download(const char* path, const char* saveTo);
void Sidecar_StreamUrl(char* out, int n, const char* bookId, int chapter, int fromMs);
