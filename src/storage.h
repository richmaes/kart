#ifndef LOGGER_STORAGE_H
#define LOGGER_STORAGE_H

#include <stddef.h>
#include <stdint.h>

#define LOG_PAGE_SIZE 4096U
#define LOG_HEADER_SIZE 32U
#define LOG_SAMPLE_SIZE 20U
#define LOG_SAMPLES_PER_PAGE 203U
#define LOG_MAGIC 0x33474c41U /* "ALG3" when read as little-endian bytes */
#define LOG_VERSION 3U
/*
 * Older pages stay readable: version 1 (accelerometer only, converted values) and
 * version 2 (accelerometer + gyroscope as converted 32-bit values, 126 per page).
 */
#define LOG_VERSION_1 1U
#define LOG_VERSION_2 2U
#define LOG_V2_SAMPLES_PER_PAGE 126U
#define LOG_SAMPLE_RATE_HZ 100U
#define LOG_RANGE_G 4U
#define LOG_ACCEL_COUNTS_PER_G 500U
#define LOG_GYRO_RANGE_DPS 2000U

/* Sample as captured in RAM; accelerometer and gyroscope are raw 16-bit counts. */
struct log_sample {
	uint64_t time_us;
	int16_t accel[3];
	int16_t gyro[3];
};

/*
 * On-flash layout, little-endian, one page per 4096-byte flash slot. See
 * docs/log-format.md; keep it, docs/thingy53_log.ksy and tools/logger.py in
 * sync with these structures.
 */
struct __attribute__((packed)) log_page_header {
	uint32_t magic;
	uint16_t version;
	uint16_t count;       /* valid samples in this page, 1..203 */
	uint32_t session;     /* recording session, starts at 1 */
	uint32_t page_index;  /* physical slot number of this page */
	uint32_t sample_rate_hz;
	uint32_t range_g;
	uint32_t gyro_range_dps; /* version 2 and 3; erased (0xffffffff) in version 1 */
	uint16_t accel_counts_per_g; /* version 3 */
	uint16_t reserved;    /* left erased (0xffff) */
};

struct __attribute__((packed)) log_sample_record {
	uint64_t time_us;     /* microseconds since the recording started */
	int16_t accel_raw[3]; /* x, y, z accelerometer counts */
	int16_t gyro_raw[3];  /* x, y, z gyroscope counts */
};

struct __attribute__((packed)) log_page_image {
	struct log_page_header header;
	struct log_sample_record samples[LOG_SAMPLES_PER_PAGE]; /* unused ones stay 0xff */
	uint32_t crc32;       /* CRC-32/IEEE of all preceding bytes of the page */
};

_Static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__, "page format is little-endian");
_Static_assert(sizeof(struct log_page_header) == LOG_HEADER_SIZE, "header size");
_Static_assert(sizeof(struct log_sample_record) == LOG_SAMPLE_SIZE, "sample size");
_Static_assert(sizeof(struct log_page_image) == LOG_PAGE_SIZE, "page size");

struct log_page {
	_Alignas(4) union {
		uint8_t data[LOG_PAGE_SIZE];
		struct log_page_image image;
	};
	uint16_t count;
	uint32_t slot;
};

struct log_storage {
	uint32_t used;
	uint32_t capacity;
	uint32_t session;
	uint32_t invalid;
};

extern struct log_storage storage;

int storage_init(void);
int storage_begin(void);
void storage_page_reset(struct log_page *page);
int storage_page_add(struct log_page *page, const struct log_sample *sample);
int storage_write_page(struct log_page *page);
int storage_erase(void);
int storage_read(uint32_t index, uint8_t *buffer);

#endif
