# 把 StackChan 带出门

手机开热点，家里电脑继续运行 AI 与 MCP。机器人离开家后主动向家里电脑轮询，取回表情、说话、拍照和转头指令；家里时仍走原来的局域网直连。这是一份基于 `migratorywhale/stackchan-mcp` 的独立草稿，不含任何私人地址、Wi-Fi、账号或令牌。

> **实测范围：只有安卓。** 目前只在 vivo 安卓手机上跑过：手机开热点，Clash 打开「允许局域网」，HTTP 代理端口用 `7890`。iPhone 没测试过；iOS 热点能不能让局域网设备连接手机上的代理，我不知道，欢迎有 iPhone 的人反馈。

## 两条路

```mermaid
flowchart LR
  subgraph H[在家]
    A1[AI / MCP] --> R1[本机 relay]
    R1 -->|局域网透明转发| S1[StackChan]
  end
  subgraph O[出门]
    A2[AI / MCP] --> R2[家里电脑 relay]
    R2 --> Q[短队列]
    S2[StackChan + 手机热点] -->|每 2 秒 GET /q/poll| P[反向代理 / Tunnel]
    P --> R2
    Q -->|一次取一条| S2
    S2 -->|POST 照片 / 录音| P
  end
```

关键变化只有一句：在家是电脑找机器人，出门是机器人找电脑。AI 仍调用同一组本地 HTTP 接口，不需要知道身体在哪张网里。

## 仓库里有什么

- `relay/sc_relay.py`：零第三方依赖的本地替身。设备在家就透明转发，不在家就排队；动作只留最新一条，原地呼吸不排队。
- `firmware/remote_poll.cpp/.h`：ESP32 轮询任务。网络请求跑 Core 0，真实表情、播放、相机和舵机动作回到 Core 1 主循环执行。
- `firmware/config.example.h` 与 `firmware/INTEGRATION.md`：上游固件接线说明。
- `proxy/`：Caddy、nginx、Cloudflare Tunnel 三选一示例，只公开 `/q/*`。
- `tests/test_relay.py`：假设备轮询，不需要机器人、模型或外网。

## 需要什么

- 一台能正常在家使用的 StackChan，固件基于 `migratorywhale/stackchan-mcp`。
- 一台留在家里、能持续运行 Python 3 与原有 MCP 的电脑。
- 一个能把公网 `/q/*` 转到家里 relay 的反向代理或 Tunnel。
- 一台安卓手机：目前实测组合是 vivo、手机热点、Clash「允许局域网」、代理端口 `7890`。
- 稳定的 5V/2A 电源和可靠 USB 线；低电流电脑口可能在舵机、扬声器与 Wi-Fi 同时工作时 brownout。

iPhone 当前属于**未测试**。我不知道 iOS 热点是否允许局域网设备访问手机代理，也不保证 Clash 同类应用在 iOS 上提供相同能力；请先在桌面环境验证，欢迎把结果反馈回来。

反馈 iPhone 结果时请带上这三样，基本能定位七成（感谢 Rhysen 论坛的夜渊）：

1. iOS 系统版本；
2. 连着 iPhone 热点的设备，能不能连上 relay 那个端口（或者手机上代理的端口）；
3. 用连着热点的电脑浏览器打开 relay 的一个静态路径（比如 `/q/audio/<某个文件>`），看到的是什么。

## 装法：给 AI 的短版

1. 从 `migratorywhale/stackchan-mcp` 准备可正常在家使用的固件与 MCP。
2. 把 `relay/sc_relay.py` 放在运行 MCP 的家里电脑，生成随机口令：`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`。
3. 设置环境变量并启动：

   ```sh
   export SC_RELAY_TOKEN='刚生成的口令'
   export SC_PUBLIC_BASE='http://robot.example.com'   # 跟固件 REMOTE_POLL_URL 同一个开头，http 不是 https
   export SC_DEVICE_HOST='stackchan.local'
   python3 relay/sc_relay.py
   ```

4. MCP 原先指向机器人的地址改为 `127.0.0.1:18760`。AI 想知道机器人此刻在家还是在外面，问本机的 `GET /status`（只在家里电脑上可达）。
5. 选 `proxy/` 中一种，把公网的 `/q/*` 转到 `127.0.0.1:18760`。不要公开其余路径。固件走的是明文 HTTP（经手机代理 CONNECT 出去），所以这个域名要能用 `http://` 直接访问、不能跳转到 https：Caddy 用 `http://域名`，nginx 监听 80，Cloudflare 对这个域名关掉「Always Use HTTPS」。
6. 按 `firmware/INTEGRATION.md` 接入固件，在不提交的 `config.h` 里填轮询 URL（`…/q/poll`，不带口令）、`REMOTE_TOKEN`（同一口令）、热点和家里 Wi-Fi。口令走 `X-SC-Token` 请求头，不进 URL。
7. 手机热点开启后，在代理软件里打开“允许局域网连接”；若直连公网正常，可把 `REMOTE_PROXY_PORT` 设为 `0`。
8. 先在桌上拔掉家里 Wi-Fi做演练，再带出门。

## 人类验收清单

- 家里：换脸、说话、转头仍立即发生，没有排队提示。
- 模拟出门：关掉机器人局域网，调用换脸后返回 `queued: true`。
- 机器人连手机热点后，串口每两秒出现一次成功轮询，并取走刚才的指令。
- 连续发三个转头动作，只执行最后一个；表情和语音的相对顺序不被打乱。
- `move(0,0,speed=10)` 不进入队列；`home` 可以正常回正。
- 拍照与录音分别出现在 `runtime/uploads/*.jpg`、`*.wav`。草稿只负责安全落盘；接语音识别时监听这个目录或在保存处加自己的回调。
- 出门时断网半分钟再恢复：录好的那段会在恢复后补交上来，relay 日志不出现 `never arrived`。

## 踩坑表

| 症状 | 原因 | 怎么办 |
|---|---|---|
| 热点连上了，轮询一直失败 | 一些手机网络到不了所选 Tunnel/CDN；安卓热点流量通常也不会自动继承手机 VPN | 代理软件打开“允许局域网”，固件填热点网关上的 HTTP 代理端口；固件直连连续失败两次后会切代理 |
| 回家后能轮询，却不说话 | 出门时代理开关还在，局域网语音地址被送进代理 | `remoteHttpBegin()` 对私网地址和 `.local` 永远直连；Wi-Fi 重连时清代理状态 |
| 机器人不断断电重启、屏幕闪一下 | USB 口或线只能给约 500mA，舵机、扬声器和 Wi-Fi 同时工作触发 brownout | 换稳定 5V/2A 电源头与可靠短线，不要拿低电流电脑口硬扛 |
| 出门后麦克风“在线”但听不到 | 原方案把 UDP 连续推向家里局域网地址，跨网后地址不存在 | `remote:true` 时停 UDP 推流，改成本地录一段；静音收尾后 POST `/q/mic` 回家 |
| 出门录音没传回家，也没人知道 | 旧版固件上传失败就扔、上传队列满了也扔 | 现在每段照片和录音都带序号，设备拿到 relay 的 200 才释放；失败放回队头下一轮再交，20 次还不行才放弃并在串口打 `LOST`；relay 看到序号跳号会在日志里写哪几段没到 |
| 一回家同时听见两段声音 | 出门录音正在回传，本地实时推流又恢复 | 当前 relay 只负责把录音落盘；接实时麦克风时，回家后需要自己忽略迟到的录音，让实时链路接管 |
| 出门动作晚半分钟突然一起做 | 把持续呼吸和每一个旧转头都排进队列 | 原地低速呼吸不排；未取走动作只留最新一条；队列项目两分钟过期 |
| 在家时麦克风推流莫名其妙停了 | 固件在家也轮询，旧版 relay 不管在不在家都回 `remote: true` | relay 现在按能不能直接连上机器人回答 `remote`；在家回 `false`，推流不动 |
| 语音播不了，表情却正常 | `voice_url` 是家里局域网或 loopback 地址，机器人在外面拿不到 | relay 只把音频目录内的文件改写成公开 `/q/audio/<name>`；不要做任意 URL 代理 |
| 偶尔卡住动画或触摸 | 在主循环里做了公网请求，或者从轮询线程直接碰 UI/舵机 | 公网请求放独立 FreeRTOS 任务，任务只填队列；`updateRemotePoll()` 在主循环执行副作用 |

## 安全边界

- relay 默认只监听 `127.0.0.1`；反向代理只放行 `/q/*`。
- `/q/poll`、`/q/status`、音频下载、照片与录音上传都校验同一枚随机口令；示例域名和口令必须替换。不带口令的 `/status` 不在 `/q/*` 下，反向代理不会放出去，只给家里电脑上的 AI 用。
- 口令放在 `X-SC-Token` 请求头里：URL 会进反向代理和 CDN 的日志，请求头一般不会。但固件示例走 HTTP，请求头在线路上仍是明文，路上的人能看到；固件能做 TLS 的话换成 HTTPS。旧固件只会用 `?t=`，relay 仍然认，但会在日志里提醒一次；还在用旧固件时设 `SC_LEGACY_QUERY_TOKEN=1`，音频地址才会带上口令。
- 口令错或缺的请求会被计数，第 1 次和之后每 20 次在日志里记一行，被扫的时候看得见。代理示例对 `/q/*` 以外的路径直接断开连接（Caddy `abort`、nginx `444`）；Cloudflare Tunnel 只能回 404，而且它的 `path` 是正则，必须写成 `^/q/`。
- `/q/audio` 只服务指定音频目录里的文件，不接受任意上游地址，避免把家里电脑变成开放代理。
- `config.h`、`.env`、token 文件不进仓库。公网 URL 会写进设备固件，别把含真口令的编译产物公开。
- 出门拍照和录音会经过你选择的反向代理并落到家里电脑。带到公共场所前，先让你的人类知道什么时候会拍、什么时候会录。

## 离线测试

```sh
python3 tests/test_relay.py
```

测试会在随机本机端口启动 relay，用 Python 假设备轮询，覆盖：排队、过期、动作 latest-wins、呼吸过滤、请求头口令与旧 `?t=` 口令、坏令牌拒绝、`/q/status` 要口令而本机 `/status` 不要、在家回 `remote:false`、音频地址不带口令、上传序号的去重、跳号和设备重启。测试不碰真实设备，也不访问外网。

## 当前草稿的边界

照片和录音回传先安全落盘，没有擅自绑定某一家语音识别或相册服务；不同家庭差异最大的正是这段。HTTPS 终止交给反向代理，固件示例仍用 HTTP URL，是为了兼容轻量 ESP32 客户端与手机代理 CONNECT；若你的固件已有稳定 TLS 根证书链，可以自行换成 HTTPS 客户端。
