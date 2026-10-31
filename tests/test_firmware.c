#include "button.h"
#include "sensor_raw.h"
#include "storage.h"
#include "writer.h"

#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/crc.h>

static uint8_t flash[LOG_PAGE_SIZE * 3];
static const struct flash_area area = {.fa_size = sizeof(flash)};
static bool fail_write;
static bool fail_erase;
static bool fail_read;

int flash_area_open(int id, const struct flash_area **result)
{
	assert(id == 0);
	*result = &area;
	return 0;
}

bool flash_area_device_is_ready(const struct flash_area *part)
{
	assert(part == &area);
	return true;
}

int flash_area_read(const struct flash_area *part, size_t offset, void *data, size_t size)
{
	assert(part == &area && offset + size <= sizeof(flash));
	if (fail_read) {
		return -EIO;
	}
	memcpy(data, flash + offset, size);
	return 0;
}

int flash_area_write(const struct flash_area *part, size_t offset, const void *data, size_t size)
{
	const uint8_t *input = data;

	assert(part == &area && offset + size <= sizeof(flash));
	for (size_t i = 0; i < (fail_write ? size / 2 : size); i++) {
		assert((flash[offset + i] & input[i]) == input[i]);
		flash[offset + i] &= input[i];
	}
	return fail_write ? -EIO : 0;
}

int flash_area_erase(const struct flash_area *part, size_t offset, size_t size)
{
	assert(part == &area && offset == 0 && size == sizeof(flash));
	if (fail_erase) {
		return -EIO;
	}
	memset(flash + offset, 0xff, size);
	return 0;
}

static void append(unsigned int count)
{
	for (unsigned int i = 0; i < count; i++) {
		struct log_sample sample = {
			.time_us = (i + 1) * 10000,
			.accel = {-1, 500, 0},
			.gyro = {100, -2, 7},
			.temperature_raw = 1024,
		};

		assert(writer_append(&sample) == 0);
	}
}

static void test_buttons(void)
{
	struct button_state state = {0};

	assert(button_update(&state, true, 0) == BUTTON_NONE);
	assert(button_update(&state, false, 5) == BUTTON_NONE);
	assert(button_update(&state, true, 10) == BUTTON_NONE);
	assert(button_update(&state, true, 40) == BUTTON_NONE);
	assert(button_update(&state, false, 100) == BUTTON_NONE);
	assert(button_update(&state, false, 130) == BUTTON_TOGGLE);
	assert(button_update(&state, false, 500) == BUTTON_NONE);
	assert(button_update(&state, true, 1000) == BUTTON_NONE);
	assert(button_update(&state, true, 1030) == BUTTON_NONE);
	assert(button_update(&state, true, 6029) == BUTTON_NONE);
	assert(button_update(&state, true, 6030) == BUTTON_ERASE);
	assert(button_update(&state, true, 9000) == BUTTON_NONE);
	assert(button_update(&state, false, 9010) == BUTTON_NONE);
	assert(button_update(&state, false, 9040) == BUTTON_NONE);
	assert(button_update(&state, true, 10000) == BUTTON_NONE);
	assert(button_update(&state, true, 10030) == BUTTON_NONE);
	assert(button_update(&state, false, 10100) == BUTTON_NONE);
	assert(button_update(&state, false, 10130) == BUTTON_TOGGLE);
	assert(button_update(&state, true, 11000) == BUTTON_NONE);
	assert(button_update(&state, true, 11030) == BUTTON_NONE);
	assert(button_update(&state, false, 16020) == BUTTON_NONE);
	assert(button_update(&state, false, 16030) == BUTTON_NONE);
	assert(button_update(&state, false, 16050) == BUTTON_TOGGLE);
}

static void test_ping_pong(void)
{
	struct log_sample sample = {.time_us = 1};

	memset(flash, 0xff, sizeof(flash));
	assert(storage_init() == 0 && writer_begin() == 0);

	/* The first full page is queued; flash is not touched by the producer. */
	append(LOG_SAMPLES_PER_PAGE);
	assert(storage.used == 0);

	/* The other RAM page keeps filling while the first one waits for flash. */
	append(LOG_SAMPLES_PER_PAGE - 1);
	assert(writer_append(&sample) == -EBUSY);

	/* A writer that cannot keep up is reported, but no sample is lost. */
	assert(writer_flush() == 0);
	assert(storage.used == 2 && storage.invalid == 0);
	assert(sys_get_le16(flash + 6) == LOG_SAMPLES_PER_PAGE);
	assert(sys_get_le32(flash + 12) == 0);
	assert(sys_get_le16(flash + LOG_PAGE_SIZE + 6) == LOG_SAMPLES_PER_PAGE);
	assert(sys_get_le32(flash + LOG_PAGE_SIZE + 12) == 1);
	assert(sys_get_le64(flash + LOG_PAGE_SIZE + LOG_HEADER_SIZE) == 10000);
	assert(sys_get_le64(flash + LOG_PAGE_SIZE + LOG_HEADER_SIZE +
			     (LOG_SAMPLES_PER_PAGE - 1) * LOG_SAMPLE_SIZE) == 1);

	/* When the writer keeps up, pages alternate without any error. */
	assert(writer_begin() == 0);
	append(LOG_SAMPLES_PER_PAGE);
	writer_process_one();
	assert(storage.used == 3 && writer_full());
	assert(writer_append(&sample) == -ENOSPC);
	assert(writer_flush() == 0);
	assert(storage_erase() == 0);
}

/* The raw conversion must exactly undo the Zephyr driver conversions. */
static void test_raw_conversion(void)
{
	for (int counts = -2048; counts <= 2047; counts++) {
		/* ADXL362 driver: counts * SENSOR_G / 500, truncated toward zero. */
		int64_t micro = counts * SENSOR_RAW_G_MICRO / ADXL362_4G_COUNTS_PER_G;

		assert(adxl362_raw_from_micro_ms2(micro) == counts);
	}
	for (int counts = INT16_MIN; counts <= INT16_MAX; counts++) {
		/* BMI270 driver: counts * 2000 * SENSOR_PI / (180 * INT16_MAX). */
		int64_t micro = (int64_t)counts * 2000 * SENSOR_RAW_PI_MICRO /
				(180LL * BMI270_GYRO_FULL_SCALE_COUNTS);

		assert(bmi270_raw_from_micro_rad_s(micro) == counts);
	}
	assert(adxl362_raw_from_micro_ms2(1000000000LL) == INT16_MAX);
	assert(adxl362_raw_from_micro_ms2(-1000000000LL) == INT16_MIN);
}

/* Pages written by version 1 firmware (accelerometer only) must stay valid. */
static void test_version_1_pages(void)
{
	uint8_t *page = flash;

	memset(flash, 0xff, sizeof(flash));
	sys_put_le32(0x33474c41U, page);
	sys_put_le16(1, page + 4);
	sys_put_le16(203, page + 6);
	sys_put_le32(5, page + 8);
	sys_put_le32(0, page + 12);
	sys_put_le32(100, page + 16);
	sys_put_le32(4, page + 20);
	sys_put_le32(crc32_ieee(page, LOG_PAGE_SIZE - 4), page + LOG_PAGE_SIZE - 4);
	assert(storage_init() == 0);
	assert(storage.used == 1 && storage.invalid == 0 && storage.session == 5);

	/* A new session appends after it, in the current format. */
	assert(writer_begin() == 0 && storage.session == 6);
	append(1);
	assert(writer_flush() == 0 && storage.used == 2);
	assert(sys_get_le16(flash + LOG_PAGE_SIZE + 4) == LOG_VERSION);
	assert(sys_get_le32(flash + LOG_PAGE_SIZE + 24) == LOG_GYRO_RANGE_DPS);
	assert(storage_init() == 0);
	assert(storage.used == 2 && storage.invalid == 0 && storage.session == 6);

	/* A version 1 page may not claim more samples than it could hold. */
	sys_put_le16(204, page + 6);
	sys_put_le32(crc32_ieee(page, LOG_PAGE_SIZE - 4), page + LOG_PAGE_SIZE - 4);
	assert(storage_init() == 0 && storage.invalid == 1);

	/* Version 3 raw pages remain readable at their original capacity. */
	sys_put_le16(3, page + 4);
	sys_put_le16(203, page + 6);
	sys_put_le32(2000, page + 24);
	sys_put_le16(500, page + 28);
	sys_put_le32(crc32_ieee(page, LOG_PAGE_SIZE - 4), page + LOG_PAGE_SIZE - 4);
	assert(storage_init() == 0 && storage.invalid == 0);
	sys_put_le16(204, page + 6);
	sys_put_le32(crc32_ieee(page, LOG_PAGE_SIZE - 4), page + LOG_PAGE_SIZE - 4);
	assert(storage_init() == 0 && storage.invalid == 1);

	/* Version 2 pages (126 converted 32-bit samples) are also still valid. */
	memset(flash, 0xff, sizeof(flash));
	sys_put_le32(0x33474c41U, page);
	sys_put_le16(2, page + 4);
	sys_put_le16(126, page + 6);
	sys_put_le32(7, page + 8);
	sys_put_le32(0, page + 12);
	sys_put_le32(100, page + 16);
	sys_put_le32(4, page + 20);
	sys_put_le32(2000, page + 24);
	sys_put_le32(crc32_ieee(page, LOG_PAGE_SIZE - 4), page + LOG_PAGE_SIZE - 4);
	assert(storage_init() == 0);
	assert(storage.used == 1 && storage.invalid == 0 && storage.session == 7);
	sys_put_le16(127, page + 6);
	sys_put_le32(crc32_ieee(page, LOG_PAGE_SIZE - 4), page + LOG_PAGE_SIZE - 4);
	assert(storage_init() == 0 && storage.invalid == 1);

	/* A current-format page with the wrong scale is rejected, not misread. */
	memset(flash, 0xff, sizeof(flash));
	assert(storage_init() == 0 && writer_begin() == 0);
	append(1);
	assert(writer_flush() == 0);
	assert(sys_get_le16(flash + 28) == LOG_ACCEL_COUNTS_PER_G);
	sys_put_le16(235, flash + 28);
	sys_put_le32(crc32_ieee(flash, LOG_PAGE_SIZE - 4), flash + LOG_PAGE_SIZE - 4);
	assert(storage_init() == 0 && storage.invalid == 1);
	assert(storage_erase() == 0);
}

int main(int argc, char **argv)
{
	assert(argc == 2);
	test_buttons();
	test_raw_conversion();
	test_ping_pong();
	test_version_1_pages();
	memset(flash, 0xff, sizeof(flash));
	assert(storage_init() == 0);
	assert(storage.capacity == 3 && storage.used == 0);
	assert(writer_begin() == 0);
	append(LOG_SAMPLES_PER_PAGE);
	assert(storage.used == 0 && !writer_full());
	writer_process_one();
	assert(storage.used == 1 && storage.session == 1);
	assert(sys_get_le16(flash + 6) == LOG_SAMPLES_PER_PAGE);
	assert(sys_get_le32(flash + 4092) == crc32_ieee(flash, 4092));
	assert(writer_flush() == 0 && storage.used == 1);

	/* Persist production-encoded data for the Python decoder to cross-check. */
	FILE *fixture = fopen(argv[1], "wb");
	assert(fixture != NULL);
	assert(fwrite(flash, 1, LOG_PAGE_SIZE, fixture) == LOG_PAGE_SIZE);
	assert(fclose(fixture) == 0);

	assert(storage_init() == 0);
	assert(storage.session == 1 && storage.used == 1);
	assert(writer_begin() == 0);
	append(1);
	assert(writer_flush() == 0);
	assert(storage.used == 2 && storage.session == 2);
	assert(sys_get_le16(flash + LOG_PAGE_SIZE + 6) == 1);
	assert(storage_init() == 0);
	assert(storage.session == 2 && storage.used == 2);

	assert(writer_begin() == 0);
	append(1);
	fail_write = true;
	assert(writer_flush() == -EIO);
	fail_write = false;
	assert(storage.used == 3 && storage.invalid == 1);
	assert(storage_init() == 0);
	assert(storage.used == 3 && storage.invalid == 1 && storage.session == 2);
	assert(writer_begin() == -ENOSPC);
	assert(storage_read(3, flash) == -EINVAL);

	fail_erase = true;
	assert(storage_erase() == -EIO);
	assert(storage.used == 3);
	fail_erase = false;
	assert(storage_erase() == 0);
	assert(storage.used == 0 && storage.session == 0 && storage.invalid == 0);
	assert(storage_init() == 0 && storage.used == 0);

	/* A dirty slot after an erased hole must also never be overwritten. */
	flash[LOG_PAGE_SIZE] = 0;
	assert(storage_init() == 0);
	assert(storage.used == 2 && storage.invalid == 1);
	assert(writer_begin() == 0);
	append(1);
	assert(writer_flush() == 0 && storage.used == 3);
	assert(storage_erase() == 0);
	storage.session = UINT32_MAX;
	assert(writer_begin() == -EOVERFLOW);
	fail_read = true;
	assert(storage_init() == -EIO);
	puts("Firmware storage and button tests passed");
	return 0;
}
