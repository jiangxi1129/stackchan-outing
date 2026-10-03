#pragma once
#include <HTTPClient.h>
#include <WiFiClient.h>

void initRemotePoll();
void updateRemotePoll();
bool remoteProxyActive();
bool remoteHttpBegin(HTTPClient& http, WiFiClient& client, const String& url);

