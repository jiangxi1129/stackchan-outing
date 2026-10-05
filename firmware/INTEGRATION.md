# 固件接入点

基线按 `migratorywhale/stackchan-mcp` 的 ESP32 固件布局写。先复制 `remote_poll.cpp/.h` 到 `firmware/src/`，再做这些小改动：

1. `main.cpp`：初始化 Wi-Fi 后调用 `initRemotePoll()`；主 `loop()` 里在 HTTP 服务附近调用 `updateRemotePoll()`。这个函数必须在主循环执行，表情、播放、舵机和相机接口不能由轮询任务直接调用。
2. `wifi_manager.cpp`：网络数组按配置顺序尝试，把手机热点放第一项、家里 Wi-Fi 放第二项；断线多次后重新遍历全部网络，不能只 `WiFi.reconnect()` 原网络。
3. `audio_download.cpp`：下载远端语音时改用 `remoteHttpBegin()`，这样轮询切到手机代理后，语音下载也走同一条路；局域网地址会强制直连。
4. `config.h`：参考 `config.example.h` 填热点、家里 Wi-Fi、公开轮询地址（以 `/q/poll` 结尾，不带口令）、`REMOTE_TOKEN` 与代理端口。这个文件不提交。旧配置把口令写成 `?t=` 跟在轮询地址后面的，照样能用，串口会提醒换成 `REMOTE_TOKEN`。
5. 若上游没有 `servo_service`、`recording_store` 或 `camera_service`，先注释对应动作；基本的表情与播放仍可用。

`RemoteCmd` 约 312 字节，8 槽队列约 2.5KB，使用普通内部堆。照片和录音本体在 PSRAM，上传队列 4 槽，每段拿到 relay 的 200 才释放；串口出现 `LOST` 说明有一段没交上去，relay 日志也会写哪几号没到。启动日志若出现 `queue allocation failed`，应减少槽数或先查内部堆，而不是把任务悄悄跑成半残。
