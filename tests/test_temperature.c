#include "imu_temperature.h"
#include <assert.h>
#include <errno.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/sys/byteorder.h>

static int result;
static uint16_t temperature;
static int sleeps;

int spi_transceive_dt(const struct spi_dt_spec *bus,
		     const struct spi_buf_set *tx, const struct spi_buf_set *rx)
{
	assert(bus->operation == 8);
	assert(tx->count == 1 && tx->buffers[0].len == 1);
	assert(*(uint8_t *)tx->buffers[0].buf == 0xa2);
	assert(rx->count == 2);
	assert(rx->buffers[0].len == 2);
	assert(rx->buffers[1].len == 2);
	/*
	 * Model nrfx's first two DMA chunks: the command clocks one byte,
	 * then its TX buffer is exhausted while one dummy RX byte remains.
	 * A null RX placeholder makes that second chunk clock zero bytes.
	 */
	size_t command_rx = rx->buffers[0].buf != NULL ? 1 : 0;
	size_t dummy_rx = rx->buffers[0].buf != NULL ? 1 : 0;

	assert(command_rx == 1 && dummy_rx == 1 &&
	       "Temperature read must clock dummy bytes after command TX ends");
	sys_put_le16(temperature, rx->buffers[1].buf);
	return result;
}

void k_usleep(int32_t microseconds)
{
	assert(microseconds == 2);
	sleeps++;
}

int main(void)
{
	int16_t raw = 123;
	int values[] = {-32767, -512, 0, 1024, 32767};

	for (unsigned int i = 0; i < sizeof(values) / sizeof(values[0]); i++) {
		temperature = (uint16_t)values[i];
		assert(imu_temperature_read(&raw) == 0 && raw == values[i]);
	}
	assert(sleeps == 5);
	temperature = 0x8000;
	assert(imu_temperature_read(&raw) == -ENODATA && raw == 32767);
	assert(sleeps == 6);
	result = -EIO;
	assert(imu_temperature_read(&raw) == -EIO && raw == 32767);
	assert(sleeps == 6);
	return 0;
}
