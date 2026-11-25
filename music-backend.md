# QQ 音乐后端部署教程

小手机的音乐应用可以连接你自己的 QQ 音乐账号：看歌单、搜歌、播放、显示歌词。浏览器不能直接访问 QQ 音乐，所以需要一个后端程序在中间转发。小手机这边只需要两样东西：**后端地址**和**访问密钥**。

> 后端代码在 `server/music/main.py`，基于开源库 [qqmusic-api-python](https://github.com/luren-dc/QQMusicApi)。你也可以自己写后端，只要符合下面「接口约定」一节，小手机就能连上。

不部署也能用音乐应用：不连接时，搜歌走 iTunes 搜索，资料库可以导入本地文件或链接。

## 一、准备

- 一台能访问公网的 Linux 服务器
- 一个域名，并解析到这台服务器。必须是 HTTPS，因为小手机网页是 HTTPS，浏览器会拦截它对 http 地址的请求
- 一个 QQ 音乐账号。歌单和登录后才能播放的歌曲都需要登录；VIP 歌曲需要账号本身有对应权限

## 二、部署

1. 把仓库里的 `server/music` 文件夹放到服务器上，比如 `/opt/qqmusic`，并安装依赖（需要 Python 3.10 或更新）：

   ```bash
   cd /opt/qqmusic
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

2. 自己生成一个访问密钥：

   ```bash
   openssl rand -hex 24
   ```

3. 先手动跑一下，确认能启动（把密钥换成你生成的）：

   ```bash
   ACCESS_KEY=你的密钥 ./venv/bin/uvicorn main:app --host 127.0.0.1 --port 8788
   ```

   环境变量：

   | 变量 | 说明 |
   | --- | --- |
   | `ACCESS_KEY` | 必填。多个密钥用英文逗号隔开，每个密钥对应一份独立的 QQ 登录和歌单，想给朋友用就多给他一个密钥 |
   | `ALLOWED_ORIGINS` | 允许的网页来源，例如 `https://你的名字.github.io`。不设置则允许所有来源，建议设置 |
   | `DATA_DIR` | 登录状态存放目录，默认 `private-data`，里面是登录凭证，不要公开 |

4. 用 systemd 保活，开机自启。`/etc/systemd/system/qqmusic.service`：

   ```ini
   [Unit]
   Description=QQ Music backend
   After=network.target

   [Service]
   WorkingDirectory=/opt/qqmusic
   Environment=ACCESS_KEY=你的密钥
   Environment=ALLOWED_ORIGINS=https://你的名字.github.io
   ExecStart=/opt/qqmusic/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8788
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now qqmusic
   ```

5. 用 Caddy 反向代理并自动申请 HTTPS 证书。Caddyfile：

   ```
   music.example.com {
       reverse_proxy 127.0.0.1:8788
   }
   ```

   把域名换成你自己的，然后 `sudo systemctl reload caddy`。服务器防火墙和云厂商的安全组要放行 80 和 443。

6. 打开 `https://music.example.com/api/music/health`，看到 `{"ok":true}` 就说明通了。

## 三、在小手机里连接

1. 打开音乐应用，在「发现」页点「去连接」。
2. 填写后端地址（`https://music.example.com`，不要带结尾的斜杠）和访问密钥。
3. 保存后如果后端还没登录 QQ 音乐，会弹出二维码。用手机 QQ 音乐扫码并点确认，3 分钟内有效。
4. 提示「QQ 音乐已连接」就完成了，发现页会显示你的歌单。

登录过期后，在音乐应用的连接菜单里点「扫码登录 QQ 音乐」重新登录。

## 四、接口约定

给自己写或改后端的人看。所有接口都在 `/api/music/` 下，请求头带 `X-Access-Key`。

| 方法 | 路径 | 请求 | 返回 |
| --- | --- | --- | --- |
| GET | `status` | | `{connected: true/false}` |
| POST | `login/start` | `{}` | `{qr: 二维码图片地址}` |
| GET | `login/status` | | `{state: "connected" / "error" / 其他表示等待中}` |
| GET | `search` | `?q=关键词&num=25` | `{songs: [歌曲]}` |
| GET | `playlists` | | `{playlists: [{id, title, songnum, cover}]}` |
| GET | `playlist` | `?id=歌单id&page=1&num=30` | `{songs: [歌曲], hasMore: true/false}` |
| POST | `song` | `{mid, mediaMid, type}` | `{url: "/开头的后端路径", direct: "可选，外部直链"}` |
| GET | `lyric` | `?mid=歌曲mid` | `{lrc: "LRC 歌词", trans: "LRC 翻译，可空"}` |

歌曲对象：

```json
{"mid": "", "title": "", "artist": "", "album": "", "art": "封面地址", "duration": 0, "mediaMid": "", "type": 0, "vip": false}
```

要点：

- **错误处理**：未登录 QQ 音乐时返回 401，且 `detail` 为 `not_logged_in`，小手机会提示扫码登录；其他 401 会被当作「访问密钥不对」。其他错误返回非 2xx，并在 `detail` 或 `error` 里放一句文字，小手机会原样显示。
- **播放地址**：`song` 返回 `direct` 时直接播放它，否则用后端地址拼上 `url` 播放。`<audio>` 播放时不能带自定义请求头，所以 `url` 指向的路径要自带校验（比如查询参数里的临时令牌），或者不做校验。
- **跨域（CORS）**：后端要允许小手机所在的网站来源，允许 `GET`、`POST`、`OPTIONS`，允许请求头 `Content-Type` 和 `X-Access-Key`，并正确响应预检请求。

## 五、常见问题

- **提示「无法连接后端，请检查网址」**：确认地址是 `https://` 开头，域名已解析，Caddy 在运行，80 和 443 端口已放行，后端进程在运行。
- **提示「访问密钥不对」**：小手机里填的密钥和后端设置的不一致，注意不要多复制空格。
- **提示「后端还没有登录 QQ 音乐」**：按第三步扫码登录。
- **歌曲没有音源或播放失败**：多半是版权或 VIP 限制，确认登录的账号有权限。
- **浏览器控制台报跨域错误**：检查后端的 CORS 设置，见第四节。

## 注意

- 后端保存着你的 QQ 音乐登录状态，**拿到地址和密钥的人就能使用你的账号**。不要把它们发给陌生人，也不要写进公开的仓库或截图里。
- 本功能仅供个人学习使用，禁止商用。请遵守 QQ 音乐的用户协议，尊重音乐版权。

## 致谢

- **薯条**：QQ 音乐后端的整体思路参考了她提供的 QQ 音乐部署文件。
- **luren-dc**：后端基于开源库 [qqmusic-api-python](https://github.com/luren-dc/QQMusicApi)（MIT 许可证，该项目声明仅供学习和参考、禁止商用）。
