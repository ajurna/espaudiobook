# Third-party code

`firmware/` files other than `espaudiobook.ino` are copied verbatim from upstream and keep their own
license. Project code (`sidecar/`, `firmware/espaudiobook.ino`) is GPL-3.0, see `LICENSE`.

| Files | License | Source |
|---|---|---|
| `firmware/es8311.*`, `firmware/es8311_reg.h`, `firmware/esp_lcd_st77916.*` | Apache-2.0, Espressif (SPDX headers preserved in the files) | https://github.com/espressif/esp_lcd, via https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.85C |
| `firmware/I2C_Driver.*`, `firmware/TCA9554PWR.*`, `firmware/Touch_CST816.*`, `firmware/Display_ST77916.*`, `firmware/LVGL_Driver.*`, `firmware/lv_conf.h` | **no license notice in the source** — Waveshare example code, left as unknown, not relicensed | https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.85C |
| LVGL 9.3.0 (library itself is not committed; installed with the Arduino library manager) | MIT | https://github.com/lvgl/lvgl |
| Montserrat fonts (`lv_font_montserrat_14/16`, used by the UI) | SIL OFL 1.1 | https://fonts.google.com/specimen/Montserrat |
| ESP32-audioI2S (library, not committed) | GPL-3.0 | https://github.com/schreibfaul1/ESP32-audioI2S |
| Arduino core / ESP32 SDK (toolchain, not committed) | LGPL-2.1 and mixed | https://github.com/espressif/arduino-esp32 |

The compiled firmware is GPL-3.0 covered because it links ESP32-audioI2S. A permissive build would mean
swapping that library for a GPL-compatible one; `espaudiobook.ino` only uses `Audio.h`'s public API, so
the swap is contained in that file.
