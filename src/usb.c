#include "usb.h"

#include <errno.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/ring_buffer.h>

static const struct device *const uart = DEVICE_DT_GET(DT_NODELABEL(board_cdc_acm_uart));
RING_BUF_DECLARE(tx, 2048);
RING_BUF_DECLARE(rx, 256);
static struct k_spinlock lock;

static void interrupt(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);
	if (!uart_irq_update(dev)) {
		return;
	}
	/* Drain all pending RX bytes, otherwise the driver re-queues this callback. */
	while (uart_irq_rx_ready(dev)) {
		uint8_t buffer[64];
		int n = uart_fifo_read(dev, buffer, sizeof(buffer));

		if (n <= 0) {
			break;
		}
		ring_buf_put(&rx, buffer, n);
	}
	if (uart_irq_tx_ready(dev)) {
		k_spinlock_key_t key = k_spin_lock(&lock);
		uint8_t *data;
		uint32_t size = ring_buf_get_claim(&tx, &data, 256);

		if (size) {
			int n = uart_fifo_fill(dev, data, size);

			ring_buf_get_finish(&tx, n > 0 ? n : 0);
		}
		if (ring_buf_is_empty(&tx)) {
			uart_irq_tx_disable(dev);
		}
		k_spin_unlock(&lock, key);
	}
}

int logger_usb_init(void)
{
	if (!device_is_ready(uart)) {
		return -ENODEV;
	}
	int rc = uart_irq_callback_user_data_set(uart, interrupt, NULL);

	if (rc) {
		return rc;
	}
	/* With a callback set, the CDC ACM driver only starts USB OUT transfers once RX is enabled. */
	uart_irq_rx_enable(uart);
	return 0;
}

bool logger_usb_connected(void)
{
	uint32_t dtr = 0;

	return uart_line_ctrl_get(uart, UART_LINE_CTRL_DTR, &dtr) == 0 && dtr;
}

bool logger_usb_send(const void *data, size_t size)
{
	if (!logger_usb_connected()) {
		return false;
	}
	k_spinlock_key_t key = k_spin_lock(&lock);
	bool fits = ring_buf_space_get(&tx) >= size;

	if (fits) {
		ring_buf_put(&tx, data, size);
		uart_irq_tx_enable(uart);
	}
	k_spin_unlock(&lock, key);
	return fits;
}

void logger_usb_discard(void)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	uart_irq_tx_disable(uart);
	ring_buf_reset(&tx);
	k_spin_unlock(&lock, key);
}

bool logger_usb_command(char *command, size_t capacity)
{
	static char input[32];
	static size_t length;
	static bool overflow;
	uint8_t c;

	while (ring_buf_get(&rx, &c, 1) == 1) {
		if (c == '\r') {
			continue;
		}
		if (c == '\n') {
			if (overflow || length >= capacity) {
				strncpy(command, "INVALID", capacity);
				command[capacity - 1] = '\0';
			} else {
				memcpy(command, input, length);
				command[length] = '\0';
			}
			length = 0;
			overflow = false;
			return true;
		}
		if (length < sizeof(input) - 1) {
			input[length++] = c;
		} else {
			overflow = true;
		}
	}
	return false;
}
