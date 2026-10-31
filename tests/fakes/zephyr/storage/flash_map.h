#ifndef TEST_FLASH_MAP_H
#define TEST_FLASH_MAP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define __aligned(n) __attribute__((aligned(n)))
#define PARTITION_ID(label) 0

struct flash_area {
	size_t fa_size;
};

int flash_area_open(int id, const struct flash_area **area);
bool flash_area_device_is_ready(const struct flash_area *area);
int flash_area_read(const struct flash_area *area, size_t offset, void *data, size_t size);
int flash_area_write(const struct flash_area *area, size_t offset, const void *data, size_t size);
int flash_area_erase(const struct flash_area *area, size_t offset, size_t size);

#endif
