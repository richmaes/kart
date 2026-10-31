#include "imu_temperature.h"

#include <errno.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>

static const struct spi_dt_spec bus = SPI_DT_SPEC_GET(
	DT_NODELABEL(bmi270), SPI_WORD_SET(8) | SPI_TRANSFER_MSB, 0);

int imu_temperature_read(int16_t *raw)
{
	uint8_t address = 0x22 | 0x80;
	uint8_t discarded[2];
	uint8_t bytes[2];
	struct spi_buf tx_buffer = {.buf = &address, .len = 1};
	struct spi_buf rx_buffers[] = {
		/* nrfx needs a real DMA buffer to clock bytes after command TX ends. */
		{.buf = discarded, .len = sizeof(discarded)},
		{.buf = bytes, .len = sizeof(bytes)},
	};
	const struct spi_buf_set tx = {.buffers = &tx_buffer, .count = 1};
	const struct spi_buf_set rx = {.buffers = rx_buffers, .count = 2};
	int rc = spi_transceive_dt(&bus, &tx, &rx);

	if (rc) {
		return rc;
	}
	k_usleep(2);
	int16_t value = (int16_t)sys_get_le16(bytes);

	if (value == INT16_MIN) {
		return -ENODATA;
	}
	*raw = value;
	return 0;
}
