#ifndef EINK_FRAME_H
#define EINK_FRAME_H

#ifndef _MSC_VER
#include <SDL2/SDL.h>
#else
#include <SDL.h>
#endif

void eink_frame_init(void);
void eink_frame_offer(SDL_Surface *surface);
void eink_frame_stop(void);

#endif
