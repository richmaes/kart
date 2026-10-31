#ifndef LOGGER_USB_H
#define LOGGER_USB_H

#include <stdbool.h>
#include <stddef.h>

int logger_usb_init(void);
bool logger_usb_connected(void);
bool logger_usb_send(const void *data, size_t size);
bool logger_usb_command(char *command, size_t capacity);
void logger_usb_discard(void);

#endif
