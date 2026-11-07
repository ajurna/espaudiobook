#include "ui.h"
#include <stdint.h>

/* This is the UI that the device draws, kept in one file so sim/main.c on PC draws the same thing.
 * The only board-specific bit is the LVGL task mutex. */
#ifdef ARDUINO
#include "LVGL_Driver.h"
#define UI_LOCK() lvgl_port_lock(0)
#define UI_UNLOCK() lvgl_port_unlock()
#else
#define UI_LOCK()
#define UI_UNLOCK()
#endif

lv_obj_t *authorsPage, *booksPage, *playerPage, *statusLbl, *titleLbl, *chapLbl, *timeLbl, *bar;
int page = 0, cursor = 0;

void ui_show_page(lv_obj_t *p) {
  UI_LOCK();
  page = p == authorsPage ? 0 : (p == booksPage ? 1 : 2);
  lv_obj_add_flag(authorsPage, LV_OBJ_FLAG_HIDDEN);
  lv_obj_add_flag(booksPage, LV_OBJ_FLAG_HIDDEN);
  lv_obj_add_flag(playerPage, LV_OBJ_FLAG_HIDDEN);
  lv_obj_clear_flag(p, LV_OBJ_FLAG_HIDDEN);
  UI_UNLOCK();
}

// Same four controls on every page: up, go, down, back. On a list, up/down move the highlighted row
// and go opens it. On the player, up/down are prev/next chapter and go is play/pause.
void ui_set_focus(lv_obj_t *list) {
  for (uint32_t i = 0; i < lv_obj_get_child_count(list); i++)
    lv_obj_remove_state(lv_obj_get_child(list, i), LV_STATE_FOCUSED);
  if (!lv_obj_get_child_count(list)) return;
  int last = (int)lv_obj_get_child_count(list) - 1;
  lv_obj_t *row = lv_obj_get_child(list, cursor > last ? last : cursor);
  lv_obj_add_state(row, LV_STATE_FOCUSED);
  lv_obj_scroll_to_view(row, LV_ANIM_OFF);      // rows are clipped by the circle, keep the cursor visible
}

// the narrowest chord in the row band (y=40..160) is 212 px, so a row that fits at the top fits everywhere
void ui_lock_row(lv_obj_t *row) {
  lv_obj_set_width(row, 210);
  lv_obj_t *l = lv_obj_get_child(row, 0);
  if (lv_obj_check_type(l, &lv_label_class)) {
    lv_label_set_long_mode(l, LV_LABEL_LONG_CLIP);
    lv_obj_set_width(l, 200);
  }
}

void ui_build(lv_event_cb_t on_button) {
  authorsPage = lv_list_create(lv_scr_act());
  booksPage = lv_list_create(lv_scr_act());
  // rows in the upper band, controls in the lower band: a 300 px row cannot share a row with a button
  lv_obj_set_size(authorsPage, 320, 170);
  lv_obj_set_size(booksPage, 320, 170);
  lv_obj_align(authorsPage, LV_ALIGN_TOP_MID, 0, 40);
  lv_obj_align(booksPage, LV_ALIGN_TOP_MID, 0, 40);
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

  // fixed control bar, offsets from the panel centre, all four inside R=160
  for (int i = 0; i < 4; i++) {
    int dx = (i == 0 ? -100 : i == 1 ? 0 : i == 2 ? 100 : 0);
    int dy = (i == 3 ? 140 : i == 1 ? 110 : 40);
    lv_obj_t *b = lv_button_create(lv_scr_act());
    lv_obj_set_size(b, 64, 64);
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_long_mode(l, LV_LABEL_LONG_CLIP);   // words stay inside the button, not past the circle
    lv_obj_set_width(l, 60);
    lv_label_set_text(l, (i == 1 ? "go" : (i == 0 ? "up" : (i == 2 ? "down" : "back"))));
    lv_obj_center(l);
    lv_obj_add_event_cb(b, on_button, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    lv_obj_align(b, LV_ALIGN_CENTER, dx, dy);
  }
  lv_obj_add_flag(playerPage, LV_OBJ_FLAG_HIDDEN);
}
