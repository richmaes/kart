#ifndef TEST_KERNEL_H
#define TEST_KERNEL_H

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

/*
 * Single-threaded stand-in for the kernel primitives used by writer.c. Blocking
 * on an empty semaphore runs the writer thread body, which models the writer
 * thread getting scheduled while the caller waits.
 */
#define ARG_UNUSED(x) (void)(x)
#define K_NO_WAIT 0
#define K_FOREVER (-1)

typedef int k_timeout_t;
typedef long atomic_t;

void writer_process_one(void);

static inline long atomic_set(atomic_t *target, long value)
{
	long old = *target;

	*target = value;
	return old;
}

static inline void atomic_clear(atomic_t *target)
{
	*target = 0;
}

static inline bool atomic_cas(atomic_t *target, long old, long value)
{
	if (*target != old) {
		return false;
	}
	*target = value;
	return true;
}

struct k_sem {
	unsigned int count;
	unsigned int limit;
};

#define K_SEM_DEFINE(name, initial, max) struct k_sem name = {initial, max}

static inline int k_sem_take(struct k_sem *sem, k_timeout_t timeout)
{
	while (!sem->count) {
		if (timeout != K_FOREVER) {
			return -EBUSY;
		}
		writer_process_one();
	}
	sem->count--;
	return 0;
}

static inline void k_sem_give(struct k_sem *sem)
{
	assert(sem->count < sem->limit);
	sem->count++;
}

struct k_msgq {
	size_t size;
	unsigned int max;
	unsigned int used;
	unsigned int read;
	unsigned int write;
	uint8_t *buffer;
};

#define K_MSGQ_DEFINE(name, size, max, align)                                  \
	static uint8_t name##_storage[(size) * (max)];                         \
	struct k_msgq name = {(size), (max), 0, 0, 0, name##_storage}

static inline int k_msgq_put(struct k_msgq *q, const void *data, k_timeout_t timeout)
{
	ARG_UNUSED(timeout);
	if (q->used == q->max) {
		return -ENOMSG;
	}
	memcpy(q->buffer + q->write * q->size, data, q->size);
	q->write = (q->write + 1) % q->max;
	q->used++;
	return 0;
}

static inline int k_msgq_get(struct k_msgq *q, void *data, k_timeout_t timeout)
{
	ARG_UNUSED(timeout);
	assert(q->used > 0 && "writer thread would block forever");
	memcpy(data, q->buffer + q->read * q->size, q->size);
	q->read = (q->read + 1) % q->max;
	q->used--;
	return 0;
}

#define K_THREAD_DEFINE(name, stack, entry, p1, p2, p3, prio, opts, delay)     \
	__attribute__((unused)) static void (*name##_entry)(void *, void *, void *) = entry

#endif
