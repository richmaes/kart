#ifndef TEST_CRC_H
#define TEST_CRC_H

#include <stddef.h>
#include <stdint.h>

static inline uint32_t crc32_ieee(const uint8_t *data, size_t size)
{
	uint32_t crc = UINT32_MAX;

	for (size_t i = 0; i < size; i++) {
		crc ^= data[i];
		for (int bit = 0; bit < 8; bit++) {
			crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320U : 0);
		}
	}
	return ~crc;
}

#endif
