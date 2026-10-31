#include "storage.h"

#include <errno.h>
#include <string.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/crc.h>

struct log_storage storage;
static const struct flash_area *area;
static uint8_t page[LOG_PAGE_SIZE] __aligned(4);

static bool erased(const uint8_t *data)
{
	for (size_t i = 0; i < LOG_PAGE_SIZE; i++) {
		if (data[i] != 0xff) {
			return false;
		}
	}
	return true;
}

static bool valid(const uint8_t *data)
{
	const struct log_page_image *image = (const void *)data;
	const struct log_page_header *h = &image->header;
	uint16_t max_count;

	switch (h->version) {
	case LOG_VERSION_1:
		max_count = LOG_V3_SAMPLES_PER_PAGE;
		break;
	case LOG_VERSION_2:
		max_count = LOG_V2_SAMPLES_PER_PAGE;
		if (h->gyro_range_dps != LOG_GYRO_RANGE_DPS) {
			return false;
		}
		break;
	case LOG_VERSION_3:
	case LOG_VERSION:
		max_count = h->version == LOG_VERSION ? LOG_SAMPLES_PER_PAGE : LOG_V3_SAMPLES_PER_PAGE;
		if (h->gyro_range_dps != LOG_GYRO_RANGE_DPS ||
		    h->accel_counts_per_g != LOG_ACCEL_COUNTS_PER_G) {
			return false;
		}
		break;
	default:
		return false;
	}
	return h->magic == LOG_MAGIC && h->count > 0 && h->count <= max_count &&
	       h->session != 0 && h->sample_rate_hz == LOG_SAMPLE_RATE_HZ &&
	       h->range_g == LOG_RANGE_G &&
	       image->crc32 == crc32_ieee(data, LOG_PAGE_SIZE - sizeof(image->crc32));
}

int storage_read(uint32_t index, uint8_t *buffer)
{
	if (index >= storage.capacity) {
		return -EINVAL;
	}
	return flash_area_read(area, index * LOG_PAGE_SIZE, buffer, LOG_PAGE_SIZE);
}

int storage_init(void)
{
	int rc = flash_area_open(PARTITION_ID(logger_partition), &area);

	if (rc) {
		return rc;
	}
	memset(&storage, 0, sizeof(storage));
	if (!flash_area_device_is_ready(area) || area->fa_size % LOG_PAGE_SIZE) {
		return -ENODEV;
	}
	storage.capacity = area->fa_size / LOG_PAGE_SIZE;
	for (uint32_t i = 0; i < storage.capacity; i++) {
		rc = storage_read(i, page);
		if (rc) {
			return rc;
		}
		if (erased(page)) {
			continue;
		}
		/* Never reuse a possibly torn write without an explicit erase. */
		storage.used = i + 1;
		if (!valid(page) || ((const struct log_page_image *)page)->header.page_index != i) {
			storage.invalid++;
			continue;
		}
		uint32_t session = ((const struct log_page_image *)page)->header.session;

		if (session > storage.session) {
			storage.session = session;
		}
	}
	return 0;
}

int storage_begin(void)
{
	if (storage.used == storage.capacity) {
		return -ENOSPC;
	}
	if (storage.session == UINT32_MAX) {
		return -EOVERFLOW;
	}
	storage.session++;
	return 0;
}

void storage_page_reset(struct log_page *p)
{
	memset(p->data, 0xff, sizeof(p->data));
	p->count = 0;
}

int storage_page_add(struct log_page *p, const struct log_sample *sample)
{
	if (p->count == LOG_SAMPLES_PER_PAGE) {
		return -ENOSPC;
	}
	struct log_sample_record *record = &p->image.samples[p->count];

	record->time_us = sample->time_us;
	record->temperature_raw = sample->temperature_raw;
	for (int i = 0; i < 3; i++) {
		record->accel_raw[i] = sample->accel[i];
		record->gyro_raw[i] = sample->gyro[i];
	}
	p->count++;
	return 0;
}

int storage_write_page(struct log_page *p)
{
	struct log_page_header *h = &p->image.header;

	h->magic = LOG_MAGIC;
	h->version = LOG_VERSION;
	h->count = p->count;
	h->session = storage.session;
	h->page_index = p->slot;
	h->sample_rate_hz = LOG_SAMPLE_RATE_HZ;
	h->range_g = LOG_RANGE_G;
	h->gyro_range_dps = LOG_GYRO_RANGE_DPS;
	h->accel_counts_per_g = LOG_ACCEL_COUNTS_PER_G;
	p->image.crc32 = crc32_ieee(p->data, LOG_PAGE_SIZE - sizeof(p->image.crc32));
	int rc = flash_area_write(area, p->slot * LOG_PAGE_SIZE, p->data, LOG_PAGE_SIZE);

	/* A failed write may have programmed some bits. Do not retry that slot. */
	storage.used = p->slot + 1;
	if (rc) {
		storage.invalid++;
	}
	return rc;
}

int storage_erase(void)
{
	int rc = flash_area_erase(area, 0, area->fa_size);

	if (rc) {
		return rc;
	}
	storage.used = 0;
	storage.session = 0;
	storage.invalid = 0;
	return 0;
}
