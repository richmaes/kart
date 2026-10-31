#include "button.h"
#include "imu_temperature.h"
#include "sensor_raw.h"
#include "storage.h"
#include "usb.h"
#include "writer.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>

static const struct device *const accel = DEVICE_DT_GET(DT_ALIAS(accel0));
static const struct device *const imu = DEVICE_DT_GET(DT_NODELABEL(bmi270));
static const struct gpio_dt_spec button = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);
static const struct gpio_dt_spec red = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec green = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
static const struct gpio_dt_spec blue = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);
static const struct sensor_trigger trigger = {
	.type = SENSOR_TRIG_DATA_READY,
	.chan = SENSOR_CHAN_ACCEL_XYZ,
};
K_MUTEX_DEFINE(capture_lock);
static bool recording;
static bool downloading;
static bool storage_ready;
static bool fatal;
static int last_error;
static int64_t erase_done_ms;
static atomic_t capture_error;
static uint64_t start_us;
static uint64_t previous_us;
static uint32_t download_page;
static uint32_t download_offset;
static int64_t download_progress;
static uint8_t download_buffer[LOG_PAGE_SIZE] __aligned(4);
static char reply[192];
static size_t reply_size;

static uint64_t now_us(void)
{
	return k_ticks_to_us_floor64(k_uptime_ticks());
}

static void data_ready(const struct device *dev, const struct sensor_trigger *trig)
{
	ARG_UNUSED(trig);
	struct sensor_value xyz[3];
	struct sensor_value gyro[3];
	struct log_sample sample;

	k_mutex_lock(&capture_lock, K_FOREVER);
	uint64_t timestamp = now_us();
	int rc = sensor_sample_fetch(dev);

	if (!rc) {
		rc = sensor_channel_get(dev, SENSOR_CHAN_ACCEL_XYZ, xyz);
	}
	/* The gyroscope runs at 200 Hz, so this read is at most 5 ms old. */
	if (!rc) {
		rc = sensor_sample_fetch(imu);
	}
	if (!rc) {
		rc = sensor_channel_get(imu, SENSOR_CHAN_GYRO_XYZ, gyro);
	}
	if (!rc) {
		rc = imu_temperature_read(&sample.temperature_raw);
	}
	if (recording && !atomic_get(&capture_error)) {
		if (!rc && previous_us && timestamp - previous_us > 15000) {
			rc = -EOVERFLOW;
		}
		previous_us = timestamp;
		if (!rc) {
			sample.time_us = timestamp - start_us;
			for (int i = 0; i < 3; i++) {
				sample.accel[i] = adxl362_raw_from_micro_ms2(sensor_value_to_micro(&xyz[i]));
				sample.gyro[i] = bmi270_raw_from_micro_rad_s(sensor_value_to_micro(&gyro[i]));
			}
			rc = writer_append(&sample);
		}
		if (rc) {
			/* Keep the first cause; later callbacks are ignored until stop. */
			atomic_cas(&capture_error, 0, rc);
		}
	}
	k_mutex_unlock(&capture_lock);
}

static void error(int rc)
{
	if (rc < 0) {
		last_error = rc;
	}
}

static void stop(void)
{
	k_mutex_lock(&capture_lock, K_FOREVER);
	recording = false;
	error(atomic_set(&capture_error, 0));
	k_mutex_unlock(&capture_lock);
	error(writer_flush());
}

static void toggle(void)
{
	if (fatal || downloading) {
		return;
	}
	if (recording) {
		stop();
		return;
	}
	int rc = writer_begin();

	if (rc) {
		error(rc);
		return;
	}
	atomic_clear(&capture_error);
	last_error = 0;
	k_mutex_lock(&capture_lock, K_FOREVER);
	start_us = now_us();
	previous_us = 0;
	recording = true;
	k_mutex_unlock(&capture_lock);
}

static void erase_blink(struct k_timer *timer)
{
	ARG_UNUSED(timer);
	gpio_pin_toggle_dt(&blue);
}

K_TIMER_DEFINE(erase_blink_timer, erase_blink, NULL);

static void erase(void)
{
	if (!storage_ready) {
		return;
	}
	if (recording) {
		stop();
	}
	downloading = false;
	reply_size = 0;
	logger_usb_discard();
	/* The erase blocks for a long time; a timer keeps blue blinking meanwhile. */
	error(gpio_pin_set_dt(&blue, 1));
	k_timer_start(&erase_blink_timer, K_MSEC(250), K_MSEC(250));
	int rc = storage_erase();

	k_timer_stop(&erase_blink_timer);

	if (rc) {
		/* Partial erase requires a reboot/re-scan before appending. */
		fatal = true;
		error(rc);
	} else {
		erase_done_ms = k_uptime_get();
		if (!fatal) {
			last_error = 0;
		}
	}
}

/* After a successful erase, blue flashes three times (150 ms off, 150 ms on). */
static bool erase_flash_active(bool *on)
{
	if (!erase_done_ms) {
		return false;
	}
	int64_t phase = (k_uptime_get() - erase_done_ms) / 150;

	if (phase >= 6) {
		erase_done_ms = 0;
		return false;
	}
	*on = phase & 1;
	return true;
}

static void poll_button(void)
{
	static struct button_state state;
	int value = gpio_pin_get_dt(&button);

	if (value < 0) {
		error(value);
		return;
	}
	switch (button_update(&state, value != 0, k_uptime_get())) {
	case BUTTON_TOGGLE:
		toggle();
		break;
	case BUTTON_ERASE:
		erase();
		break;
	case BUTTON_NONE:
		break;
	}
}

static void poll_usb(void)
{
	char command[32];

	if (!logger_usb_connected()) {
		downloading = false;
		reply_size = 0;
		logger_usb_discard();
		return;
	}
	if (reply_size) {
		if (logger_usb_send(reply, reply_size)) {
			reply_size = 0;
		}
		return;
	}
	if (downloading) {
		if (k_uptime_get() - download_progress > 10000) {
			downloading = false;
			logger_usb_discard();
			error(-ETIMEDOUT);
			return;
		}
		if (download_page == storage.used) {
			memcpy(reply, "END\n", 4);
			reply_size = 4;
			downloading = false;
			return;
		}
		if (!download_offset) {
			int rc = storage_read(download_page, download_buffer);

			if (rc) {
				error(rc);
				downloading = false;
				reply_size = snprintf(reply, sizeof(reply), "ERR read %d\n", rc);
				return;
			}
		}
		if (logger_usb_send(download_buffer + download_offset, 256)) {
			download_progress = k_uptime_get();
			download_offset += 256;
			if (download_offset == LOG_PAGE_SIZE) {
				download_page++;
				download_offset = 0;
			}
		}
		return;
	}
	if (!logger_usb_command(command, sizeof(command))) {
		return;
	}
	if (!strcmp(command, "STATUS")) {
		reply_size = snprintf(reply, sizeof(reply),
			"STATUS %s pages=%u capacity=%u session=%u invalid=%u error=%d\n",
			fatal ? "fault" : recording ? "recording" : "stopped",
			storage.used, storage.capacity, storage.session,
			storage.invalid, last_error);
	} else if (!strcmp(command, "DUMP")) {
		if (recording || fatal) {
			reply_size = snprintf(reply, sizeof(reply), "ERR %s\n",
					      recording ? "recording" : "fault");
		} else {
			downloading = true;
			download_page = 0;
			download_offset = 0;
			download_progress = k_uptime_get();
			reply_size = snprintf(reply, sizeof(reply), "BEGIN %u\n", storage.used);
		}
	} else {
		reply_size = snprintf(reply, sizeof(reply), "ERR unknown-command\n");
	}
}

static int configure_gyro(void)
{
	struct sensor_value odr = {.val1 = 200};
	struct sensor_value range = {.val1 = LOG_GYRO_RANGE_DPS};
	int rc = sensor_attr_set(imu, SENSOR_CHAN_GYRO_XYZ, SENSOR_ATTR_FULL_SCALE, &range);

	if (!rc) {
		rc = sensor_attr_set(imu, SENSOR_CHAN_GYRO_XYZ, SENSOR_ATTR_SAMPLING_FREQUENCY, &odr);
	}
	return rc;
}

int main(void)
{
	const struct gpio_dt_spec *leds[] = {&red, &green, &blue};
	int rc;

	for (size_t i = 0; i < ARRAY_SIZE(leds); i++) {
		if (!gpio_is_ready_dt(leds[i])) {
			return -ENODEV;
		}
		rc = gpio_pin_configure_dt(leds[i], GPIO_OUTPUT_INACTIVE);
		if (rc) {
			return rc;
		}
	}
	rc = logger_usb_init();
	if (rc) {
		error(rc);
		fatal = true;
	}
	rc = storage_init();
	storage_ready = rc == 0;
	if (rc) {
		error(rc);
		fatal = true;
	}
	if (!gpio_is_ready_dt(&button)) {
		rc = -ENODEV;
	} else {
		rc = gpio_pin_configure_dt(&button, GPIO_INPUT);
	}
	if (!rc && (!device_is_ready(accel) || !device_is_ready(imu))) {
		rc = -ENODEV;
	}
	if (!rc) {
		rc = configure_gyro();
	}
	if (!rc) {
		rc = sensor_trigger_set(accel, &trigger, data_ready);
	}
	if (rc) {
		error(rc);
		fatal = true;
	}
	while (true) {
		if (recording) {
			int capture_rc = atomic_set(&capture_error, 0);

			error(writer_take_error());
			if (capture_rc || last_error || writer_full()) {
				error(capture_rc);
				if (!last_error) {
					error(-ENOSPC);
				}
				stop();
			}
		}
		poll_button();
		poll_usb();
		error(gpio_pin_set_dt(&red, last_error != 0 || storage.invalid != 0));
		error(gpio_pin_set_dt(&green, recording));
		bool blue_on = downloading || (!recording && !last_error);
		bool flash_on;

		if (erase_flash_active(&flash_on)) {
			blue_on = flash_on;
		}
		error(gpio_pin_set_dt(&blue, blue_on));
		k_sleep(K_MSEC(1));
	}
	return 0;
}
