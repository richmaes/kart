#include "button.h"

enum button_event button_update(struct button_state *state, bool active, int64_t now_ms)
{
	if (active != state->raw) {
		state->raw = active;
		state->changed = now_ms;
	}
	if (state->raw != state->stable && now_ms - state->changed >= 30) {
		state->stable = state->raw;
		if (state->stable) {
			state->pressed = now_ms;
			state->held = false;
		} else if (!state->held) {
			return BUTTON_TOGGLE;
		}
	}
	if (state->raw && state->stable && !state->held &&
	    now_ms - state->pressed >= 5000) {
		state->held = true;
		return BUTTON_ERASE;
	}
	return BUTTON_NONE;
}
