#ifndef LOGGER_WRITER_H
#define LOGGER_WRITER_H

#include "storage.h"

#include <stdbool.h>

/* Start a session: reserves a session id and resets both RAM pages. */
int writer_begin(void);

/*
 * Producer side, called by one thread at a time while recording. Never blocks
 * on flash. Returns -EBUSY if a full page could not be handed over because the
 * writer thread is still busy with the previous page, and -ENOSPC when full.
 */
int writer_append(const struct log_sample *sample);

/* Hand over the partial page and wait until all pages are in flash. */
int writer_flush(void);

/* Returns and clears the first flash write error reported by the writer thread. */
int writer_take_error(void);

/* True once every page slot is written or queued for writing. */
bool writer_full(void);

/* Consumer side; runs forever in the writer thread. Exposed for host tests. */
void writer_process_one(void);

#endif
