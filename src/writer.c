#include "writer.h"

#include <errno.h>
#include <zephyr/kernel.h>

/*
 * Two RAM pages ping-pong: the producer fills pages[fill] while the writer
 * thread programs the other one into flash. A single token (spare) represents
 * the page that is free to switch to, so ownership never overlaps and no lock
 * is held while flash is busy.
 */
static struct log_page pages[2];
static struct log_page *fill = &pages[0];
static uint32_t reserved;
static atomic_t write_error;

K_SEM_DEFINE(spare, 1, 1);
K_MSGQ_DEFINE(ready, sizeof(struct log_page *), 1, sizeof(struct log_page *));

static int submit(k_timeout_t wait)
{
	if (reserved == storage.capacity) {
		return -ENOSPC;
	}
	if (k_sem_take(&spare, wait)) {
		return -EBUSY;
	}
	fill->slot = reserved++;
	k_msgq_put(&ready, &fill, K_NO_WAIT);
	fill = fill == &pages[0] ? &pages[1] : &pages[0];
	return 0;
}

int writer_begin(void)
{
	int rc = storage_begin();

	if (rc) {
		return rc;
	}
	reserved = storage.used;
	atomic_clear(&write_error);
	storage_page_reset(&pages[0]);
	storage_page_reset(&pages[1]);
	fill = &pages[0];
	return 0;
}

int writer_append(const struct log_sample *sample)
{
	if (reserved == storage.capacity) {
		return -ENOSPC;
	}
	int rc = storage_page_add(fill, sample);

	if (rc == 0 && fill->count == LOG_SAMPLES_PER_PAGE) {
		return submit(K_NO_WAIT);
	}
	return rc;
}

int writer_flush(void)
{
	int rc = 0;

	if (fill->count) {
		rc = submit(K_FOREVER);
	}
	/* The spare token comes back only after the last queued page is written. */
	k_sem_take(&spare, K_FOREVER);
	k_sem_give(&spare);
	int write_rc = writer_take_error();

	return rc ? rc : write_rc;
}

int writer_take_error(void)
{
	return atomic_set(&write_error, 0);
}

bool writer_full(void)
{
	return reserved == storage.capacity;
}

void writer_process_one(void)
{
	struct log_page *page;

	k_msgq_get(&ready, &page, K_FOREVER);
	int rc = storage_write_page(page);

	if (rc) {
		atomic_cas(&write_error, 0, rc);
	}
	storage_page_reset(page);
	k_sem_give(&spare);
}

static void writer_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);
	while (true) {
		writer_process_one();
	}
}

/* Lower priority than the accelerometer thread (10) so sampling always preempts it. */
K_THREAD_DEFINE(writer_tid, 2048, writer_thread, NULL, NULL, NULL, 11, 0, 0);
