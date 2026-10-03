#pragma once

// Keep the real file out of git. Put the phone hotspot first, home Wi-Fi second.
#define WIFI_NETWORK_COUNT 2
#define WIFI_SSID_0 "PHONE_HOTSPOT_NAME"
#define WIFI_PASSWORD_0 "PHONE_HOTSPOT_PASSWORD"
#define WIFI_SSID_1 "HOME_WIFI_NAME"
#define WIFI_PASSWORD_1 "HOME_WIFI_PASSWORD"

// Public reverse proxy → local sc_relay.py. Generate a long random token.
#define REMOTE_POLL_URL "http://relay.example.invalid/q/poll?t=REPLACE_WITH_RANDOM_TOKEN"

// Clash-compatible HTTP proxy exposed on the phone hotspot LAN. Set 0 if direct access works.
#define REMOTE_PROXY_PORT 7890

