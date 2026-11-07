/* Does lv_timer_handler() paint when called under lv_lock()? */
#include <Windows.h>
#include <stdio.h>
#include <lvgl.h>

int main(void) {
  lv_init();
  lv_display_t *disp = lv_windows_create_display(L"PoC locked", 360, 360, 100, false, true);
  if (!disp) return 1;
  lv_windows_acquire_pointer_indev(disp);

  lv_lock();
  lv_obj_t *lbl = lv_label_create(lv_screen_active());
  lv_label_set_text(lbl, "locked handler");
  lv_obj_center(lbl);
  lv_unlock();

  for (int i = 0; i < 25; i++) {
    lv_lock();
    uint32_t t = lv_timer_handler();
    lv_unlock();
    if (t == LV_NO_TIMER_READY) t = LV_DEF_REFR_PERIOD;
    Sleep(t);
  }
  lv_draw_buf_t *b = lv_display_get_buf_active(disp);
  int on = 0;
  for (uint32_t i = 0; i + 3 < b->data_size; i += 4) if (b->data[i] || b->data[i+1] || b->data[i+2]) on++;
  printf("locked-handler painted %d of %u\n", on, (unsigned)b->data_size / 4);
  return 0;
}
