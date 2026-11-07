#include <lvgl.h>
#include <windows.h>
#include "ui.h"

/* Same UI as the sketch, on a 360x360 window, with placeholder rows: no Wi-Fi, no sidecar,
 * no audio. Run sim/build.sh, then click the four controls. */

static void sample(lv_obj_t *list, const char *rows[]) {
  for (int i = 0; rows[i]; i++) {
    lv_obj_t *row = lv_list_add_button(list, NULL, rows[i]);
    ui_lock_row(row);                       // the clipping rule the firmware uses
  }
}

static void on_button(lv_event_t *e) {
  int what = (int)(intptr_t)lv_event_get_user_data(e);
  if (what == 3) { ui_show_page(page == 2 ? booksPage : authorsPage); return; }
  if (page == 2) { ui_show_page(booksPage); return; }            // back to the list
  if (page == 1 && lv_obj_get_child_count(booksPage)) {          // go: open the focused book
    lv_label_set_text(titleLbl, lv_label_get_text(lv_obj_get_child(lv_obj_get_child(booksPage, cursor), 0)));
    ui_show_page(playerPage);
    return;
  }
  int n = (int)lv_obj_get_child_count(page == 1 ? booksPage : authorsPage);
  if (!n) return;
  cursor = cursor + (what == 0 ? -1 : 1);
  if (cursor < 0) cursor = 0;
  if (cursor > n - 1) cursor = n - 1;
  ui_set_focus(page == 1 ? booksPage : authorsPage);
}

int main(void) {
  lv_init();
  lv_display_t *disp = lv_windows_create_display(L"espaudiobook 360x360", 360, 360, 2, true, true);
  lv_windows_acquire_pointer_indev(disp);

  static const char *authors[] = {"Erin Hunter (121)", "Ann Leckie (9)", "Lois McMaster Bujold (13)", 0};
  static const char *books[] = {"The Lost Warrior", "The Broken Code", "A Dangerous Woman", 0};
  ui_build(on_button);
  sample(authorsPage, authors);
  sample(booksPage, books);
  lv_label_set_text(titleLbl, "The Lost Warrior");
  lv_label_set_text(chapLbl, "Chapter 12 - Climbing Rocks");
  lv_label_set_text(timeLbl, "14:20 / 41:05");
  lv_bar_set_value(bar, 35, LV_ANIM_OFF);
  ui_set_focus(authorsPage);

  while (1) {
    lv_timer_handler();
    Sleep(5);
  }
  return 0;
}
