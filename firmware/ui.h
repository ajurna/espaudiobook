#pragma once
#include <lvgl.h>

/* UI state and layout, shared by the sketch and sim/main.c (PC simulator). */
extern lv_obj_t *authorsPage, *booksPage, *playerPage, *statusLbl, *titleLbl, *chapLbl, *timeLbl, *bar;
extern int page, cursor;

void ui_build(lv_event_cb_t on_button);          // buttons call the host's handler
void ui_show_page(lv_obj_t *p);
void ui_lock_row(lv_obj_t *row);
void ui_set_focus(lv_obj_t *list);
