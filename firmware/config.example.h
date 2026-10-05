#pragma once

// Keep the real file out of git. Put the phone hotspot first, home Wi-Fi second.
#define WIFI_NETWORK_COUNT 2
#define WIFI_SSID_0 "PHONE_HOTSPOT_NAME"
#define WIFI_PASSWORD_0 "PHONE_HOTSPOT_PASSWORD"
#define WIFI_SSID_1 "HOME_WIFI_NAME"
#define WIFI_PASSWORD_1 "HOME_WIFI_PASSWORD"

// Public reverse proxy → local sc_relay.py.
#define REMOTE_POLL_URL "http://robot.example.com/q/poll"
// Same long random token as the relay. Sent as the X-SC-Token header so it never sits in a URL
// (URLs end up in reverse-proxy and CDN logs). Configs that still put ?t=TOKEN on REMOTE_POLL_URL keep working.
#define REMOTE_TOKEN "REPLACE_WITH_RANDOM_TOKEN"

// Clash-compatible HTTP proxy exposed on the phone hotspot LAN. Set 0 if direct access works.
#define REMOTE_PROXY_PORT 7890

