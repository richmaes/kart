#ifndef TEST_SPI_H
#define TEST_SPI_H

#include <stddef.h>

#define DT_NODELABEL(name) 0
#define SPI_WORD_SET(bits) (bits)
#define SPI_TRANSFER_MSB 0
#define SPI_DT_SPEC_GET(node, op, delay) {.operation = op}

struct spi_dt_spec {
	int operation;
};
struct spi_buf {
	void *buf;
	size_t len;
};
struct spi_buf_set {
	struct spi_buf *buffers;
	size_t count;
};

int spi_transceive_dt(const struct spi_dt_spec *bus,
		     const struct spi_buf_set *tx, const struct spi_buf_set *rx);

#endif
