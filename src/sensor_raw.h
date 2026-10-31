#ifndef LOGGER_SENSOR_RAW_H
#define LOGGER_SENSOR_RAW_H

#include <stdint.h>

/*
 * Zephyr's sensor API only returns converted values. These helpers undo the
 * drivers' integer conversions so the raw 16-bit counts can be stored. One
 * count is about 19,600 micro-m/s^2 or 1,065 micro-rad/s, far larger than the
 * drivers' rounding error, so rounding to the nearest count is exact.
 */
#define SENSOR_RAW_G_MICRO 9806650LL      /* Zephyr SENSOR_G */
#define SENSOR_RAW_PI_MICRO 3141592LL     /* Zephyr SENSOR_PI */
#define ADXL362_4G_COUNTS_PER_G 500LL     /* ADXL362 driver, +/-4 g range */
#define BMI270_GYRO_FULL_SCALE_COUNTS 32767LL /* BMI270 driver, INT16_MAX */

static inline int16_t sensor_raw_clamp(int64_t counts)
{
	return counts > INT16_MAX ? INT16_MAX : counts < INT16_MIN ? INT16_MIN : (int16_t)counts;
}

static inline int64_t sensor_raw_divide_round(int64_t numerator, int64_t denominator)
{
	return (numerator + (numerator < 0 ? -denominator / 2 : denominator / 2)) / denominator;
}

/* ADXL362 acceleration in micro-m/s^2 to raw counts (driver: counts * G / 500). */
static inline int16_t adxl362_raw_from_micro_ms2(int64_t micro_ms2)
{
	return sensor_raw_clamp(sensor_raw_divide_round(micro_ms2 * ADXL362_4G_COUNTS_PER_G,
							SENSOR_RAW_G_MICRO));
}

/* BMI270 rate in micro-rad/s to raw counts at +/-2000 deg/s. */
static inline int16_t bmi270_raw_from_micro_rad_s(int64_t micro_rad_s)
{
	return sensor_raw_clamp(sensor_raw_divide_round(
		micro_rad_s * 180LL * BMI270_GYRO_FULL_SCALE_COUNTS,
		2000LL * SENSOR_RAW_PI_MICRO));
}

#endif
