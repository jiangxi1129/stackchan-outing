#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include "remote_poll.h"
#include "config.h"

// These headers/functions are from migratorywhale/stackchan-mcp. If a newer
// upstream renamed one, adjust only this adapter section.
#include "face_names.h"
#include "face_service.h"
#include "playback_service.h"
#include "servo_service.h"
#include "mic_service.h"
#include "recording_store.h"
#include "camera_service.h"
#include "types.h"

#ifndef REMOTE_POLL_URL
#define REMOTE_POLL_URL ""
#endif
#ifndef REMOTE_PROXY_PORT
#define REMOTE_PROXY_PORT 0
#endif
// New configs put the token here; it travels in the X-SC-Token header, never in a URL.
// Old configs that still end REMOTE_POLL_URL with ?t=... keep working without it.
#ifndef REMOTE_TOKEN
#define REMOTE_TOKEN ""
#endif

static constexpr uint32_t POLL_MS = 2000;
static constexpr uint32_t BACKOFF_MAX_MS = 30000;
static constexpr uint32_t HTTP_TIMEOUT_MS = 8000;
static constexpr size_t URL_MAX = 256;
static constexpr uint8_t UPLOAD_TRIES_MAX = 20;   // ~a few minutes of retries across polls, then give up loudly

struct RemoteCmd {
    char face[32];
    char url[URL_MAX];
    bool snapshot;
    char motion[8];
    float x, y;
    int speed;
};
// A photo or recording waits in PSRAM until the relay answers 200. seq lets the relay spot gaps and duplicates.
struct Upload { uint8_t* data; size_t size; bool jpeg; uint32_t seq; uint8_t tries; };

static QueueHandle_t commandQueue = nullptr;
static QueueHandle_t uploadQueue = nullptr;
static volatile bool useProxy = false;
static volatile bool remote = false;
static uint32_t photoSeq = 0, micSeq = 0;
static uint32_t bootId = 0;
static String remoteBase;    // REMOTE_POLL_URL up to /q/poll
static String legacyQuery;   // what followed ? in REMOTE_POLL_URL (old configs: t=TOKEN)

bool remoteProxyActive() { return useProxy; }

static bool splitHttpUrl(const String& url, String& host, uint16_t& port, String& path) {
    if (!url.startsWith("http://")) return false;
    int hostStart = 7, pathAt = url.indexOf('/', hostStart);
    String hostPort = pathAt < 0 ? url.substring(hostStart) : url.substring(hostStart, pathAt);
    path = pathAt < 0 ? "/" : url.substring(pathAt);
    int colon = hostPort.indexOf(':');
    host = colon < 0 ? hostPort : hostPort.substring(0, colon);
    port = colon < 0 ? 80 : (uint16_t)hostPort.substring(colon + 1).toInt();
    return host.length() > 0;
}

static bool privateHost(const String& host) {
    if (host.startsWith("10.") || host.startsWith("192.168.") || host.startsWith("127.") || host == "localhost") return true;
    if (host.startsWith("172.")) {
        int dot = host.indexOf('.', 4), second = host.substring(4, dot).toInt();
        return second >= 16 && second <= 31;
    }
    return host.endsWith(".local");
}

static bool proxyTunnel(WiFiClient& client, const String& host, uint16_t port) {
    if (client.connected()) return true;
    if (!client.connect(WiFi.gatewayIP(), REMOTE_PROXY_PORT, 5000)) return false;
    client.printf("CONNECT %s:%u HTTP/1.1\r\nHost: %s:%u\r\n\r\n", host.c_str(), port, host.c_str(), port);
    bool gotStatus = false, ok = false;
    unsigned long started = millis();
    while (millis() - started < 5000) {
        if (!client.available()) { delay(5); continue; }
        String line = client.readStringUntil('\n'); line.trim();
        if (!gotStatus) { gotStatus = true; ok = line.startsWith("HTTP/1.") && line.indexOf(" 200") > 0; }
        if (!line.length()) break;
    }
    if (!ok) client.stop();
    return ok;
}

static bool beginPlain(HTTPClient& http, WiFiClient& client, const String& url) {
    if (!useProxy || !REMOTE_PROXY_PORT) return http.begin(client, url);
    String host, path; uint16_t port;
    if (!splitHttpUrl(url, host, port, path)) return false;
    if (privateHost(host)) return http.begin(client, url);
    if (!proxyTunnel(client, host, port)) return false;
    return http.begin(client, host, port, path, false);
}

bool remoteHttpBegin(HTTPClient& http, WiFiClient& client, const String& url) {
    if (!beginPlain(http, client, url)) return false;
    // Only our relay gets the token. begin() resets headers, so this has to come after it.
    // Match scheme://host[:port][prefix] *and* the /q/ path boundary, so http://relay.example.evil/... never gets it.
    if (*REMOTE_TOKEN && remoteBase.length() && url.startsWith(remoteBase + "/q/")) http.addHeader("X-SC-Token", REMOTE_TOKEN);
    return true;
}

// Returns the HTTP status (200 = saved), or a negative number for a network error.
static int upload(HTTPClient& http, WiFiClient& client, const Upload& item) {
    String url = remoteBase + (item.jpeg ? "/q/photo?" : "/q/mic?");
    if (legacyQuery.length()) url += legacyQuery + "&";
    url += "seq=" + String(item.seq) + "&boot=" + String(bootId, HEX);
    if (!remoteHttpBegin(http, client, url)) return -1;
    http.addHeader("Content-Type", item.jpeg ? "image/jpeg" : "audio/wav");
    int code = http.POST(item.data, item.size); http.end();
    Serial.printf("[OUTING] upload %s #%u %u bytes -> %d\n", item.jpeg ? "photo" : "recording", (unsigned)item.seq, (unsigned)item.size, code);
    return code;
}

// The upload being worked on lives here, owned by the poll task, not in the queue: taking it out of the queue
// and putting it back later could lose it if the main loop filled the freed slot during an 8-second POST.
// Order is kept (one at a time, oldest first). Network errors and 5xx/408/429 are retried on later polls,
// up to UPLOAD_TRIES_MAX; any other 4xx will never succeed, so it is dropped at once. Every drop prints LOST,
// and the relay sees the missing seq and logs the gap on its side.
static Upload current;
static bool haveCurrent = false;
static void drainUploads(HTTPClient& http, WiFiClient& client) {
    for (;;) {
        if (!haveCurrent) {
            if (!uploadQueue || xQueueReceive(uploadQueue, &current, 0) != pdTRUE) return;
            haveCurrent = true;
        }
        int code = upload(http, client, current);
        const char* what = current.jpeg ? "photo" : "recording";
        if (code == HTTP_CODE_OK) { free(current.data); haveCurrent = false; continue; }
        bool permanent = code >= 400 && code < 500 && code != 408 && code != 429;
        if (permanent || ++current.tries >= UPLOAD_TRIES_MAX) {
            Serial.printf("[OUTING] LOST %s #%u: %s (last status %d)\n", what, (unsigned)current.seq,
                          permanent ? "relay refused it" : "gave up retrying", code);
            free(current.data); haveCurrent = false; continue;
        }
        return;   // keep it, try again on the next poll; a dead link is not hammered
    }
}

static void pollTask(void*) {
    WiFiClient client; HTTPClient http; http.setReuse(true); http.setTimeout(HTTP_TIMEOUT_MS);
    uint32_t wait = POLL_MS; int failures = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(wait));
        if (WiFi.status() != WL_CONNECTED) { wait = POLL_MS; useProxy = false; client.stop(); continue; }
        drainUploads(http, client);
        if (!remoteHttpBegin(http, client, REMOTE_POLL_URL)) { wait = BACKOFF_MAX_MS; continue; }
        int code = http.GET();
        if (code != HTTP_CODE_OK) {
            http.end(); failures++;
            if (REMOTE_PROXY_PORT && failures >= 2) { useProxy = !useProxy; failures = 0; client.stop(); wait = POLL_MS; continue; }
            wait = min(wait * 2, BACKOFF_MAX_MS); continue;
        }
        String body = http.getString(); http.end(); failures = 0; wait = POLL_MS;
        JsonDocument doc;
        if (deserializeJson(doc, body) != DeserializationError::Ok) continue;
        remote = doc["remote"] | false;
        const char* face = doc["face"] | "";
        const char* voice = doc["voice_url"] | "";
        const char* path = doc["path"] | "";
        const char* motion = doc["motion"] | "";
        bool snapshot = strcmp(path, "/snapshot") == 0;
        if (!*face && !*voice && !snapshot && !*motion) continue;
        RemoteCmd cmd = {};
        strlcpy(cmd.face, face, sizeof(cmd.face)); strlcpy(cmd.motion, motion, sizeof(cmd.motion));
        cmd.snapshot = snapshot; cmd.x = doc["x"] | 0.0f; cmd.y = doc["y"] | 0.0f; cmd.speed = doc["speed"] | 50;
        if (strlen(voice) < sizeof(cmd.url)) strlcpy(cmd.url, voice, sizeof(cmd.url));
        else Serial.println("[OUTING] voice URL too long; omitted");
        if (xQueueSend(commandQueue, &cmd, 0) != pdTRUE) Serial.println("[OUTING] command queue full; dropped");
    }
}

void initRemotePoll() {
    if (!*REMOTE_POLL_URL) return;
    String url = REMOTE_POLL_URL;
    int q = url.indexOf('?');
    String path = q < 0 ? url : url.substring(0, q);
    if (!url.startsWith("http://") || !path.endsWith("/q/poll") || path.length() <= 14) {   // http://x + /q/poll
        Serial.println("[OUTING] REMOTE_POLL_URL must look like http://host[:port][/prefix]/q/poll[?...]; outing disabled");
        return;
    }
    remoteBase = path.substring(0, path.length() - 7);   // strip "/q/poll"
    legacyQuery = q < 0 ? String() : url.substring(q + 1);
    if (legacyQuery.length() && *REMOTE_TOKEN) Serial.println("[OUTING] REMOTE_TOKEN is set; drop ?t= from REMOTE_POLL_URL so the token stays out of URLs");
    bootId = esp_random();
    commandQueue = xQueueCreate(8, sizeof(RemoteCmd));
    uploadQueue = xQueueCreate(4, sizeof(Upload));
    if (!commandQueue || !uploadQueue) { Serial.println("[OUTING] queue allocation failed"); return; }
    xTaskCreatePinnedToCore(pollTask, "outing_poll", 8192, nullptr, 1, nullptr, 0);
}

void updateRemotePoll() {
    if (!commandQueue) return;
    if (remote) {
        if (isMicStreaming()) setMicStreamTarget("", 0);
        if (hasLastRecording()) {
            RecordingSnapshot source = getLastRecording();
            uint32_t seq = ++micSeq;   // numbered even if it is lost below, so the relay sees the gap
            uint8_t* copy = (uint8_t*)ps_malloc(source.size);
            if (!copy) Serial.printf("[OUTING] LOST recording #%u: no PSRAM for a %u-byte copy\n", (unsigned)seq, (unsigned)source.size);
            else {
                memcpy(copy, source.data, source.size); Upload item = {copy, source.size, false, seq, 0};
                if (xQueueSend(uploadQueue, &item, 0) != pdTRUE) { free(copy); Serial.printf("[OUTING] LOST recording #%u: upload queue full\n", (unsigned)seq); }
            }
            markLastRecordingConsumed();
        }
    }
    RemoteCmd cmd;
    while (xQueueReceive(commandQueue, &cmd, 0) == pdTRUE) {
        if (cmd.snapshot) {
            uint8_t* jpeg = nullptr; size_t size = 0;
            if (captureJpeg(&jpeg, &size, 80)) {
                Upload item = {jpeg, size, true, ++photoSeq, 0};
                if (xQueueSend(uploadQueue, &item, 0) != pdTRUE) { free(jpeg); Serial.printf("[OUTING] LOST photo #%u: upload queue full\n", (unsigned)item.seq); }
            }
        }
        if (cmd.motion[0]) {
            if (!strcmp(cmd.motion, "move")) servoMove(cmd.x, cmd.y, cmd.speed);
            else if (!strcmp(cmd.motion, "nod")) servoNod();
            else if (!strcmp(cmd.motion, "shake")) servoShake();
            else if (!strcmp(cmd.motion, "home")) servoHome(cmd.speed);
        }
        if (cmd.face[0]) { WhaleFace face; if (whaleFaceFromName(cmd.face, &face)) setWhaleFace(face); }
        if (cmd.url[0]) {
            AudioTask task; task.voice_id = String("remote_") + millis(); task.voice_url = String(cmd.url); task.priority = PRIORITY_NORMAL;
            enqueueAudioTask(task);
        }
    }
}

