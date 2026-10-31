#ifndef TEST_BYTEORDER_H
#define TEST_BYTEORDER_H

#include <stdint.h>

static inline void sys_put_le16(uint16_t value, uint8_t *data)
{
	for (int i = 0; i < 2; i++) {
		data[i] = value >> (i * 8);
	}
}

static inline void sys_put_le32(uint32_t value, uint8_t *data)
{
	for (int i = 0; i < 4; i++) {
		data[i] = value >> (i * 8);
	}
}

static inline void sys_put_le64(uint64_t value, uint8_t *data)
{
	for (int i = 0; i < 8; i++) {
		data[i] = value >> (i * 8);
	}
}

static inline uint16_t sys_get_le16(const uint8_t *data)
{
	return (uint16_t)data[0] | (uint16_t)data[1] << 8;
}

static inline uint32_t sys_get_le32(const uint8_t *data)
{
	uint32_t value = 0;

	for (int i = 0; i < 4; i++) {
		value |= (uint32_t)data[i] << (i * 8);
	}
	return value;
}

static inline uint64_t sys_get_le64(const uint8_t *data)
{
	return (uint64_t)sys_get_le32(data) | (uint64_t)sys_get_le32(data + 4) << 32;
}

#endif
