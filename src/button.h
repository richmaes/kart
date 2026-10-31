#ifndef LOGGER_BUTTON_H
#define LOGGER_BUTTON_H

#include <stdbool.h>
#include <stdint.h>

enum button_event {
	BUTTON_NONE,
	BUTTON_TOGGLE,
	BUTTON_ERASE,
};

struct button_state {
	bool raw;
	bool stable;
	bool held;
	int64_t changed;
	int64_t pressed;
};

enum button_event button_update(struct button_state *state, bool active, int64_t now_ms);

#endif
