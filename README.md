# Stream247 Multi-Destination Panel

لوحة ويب خفيفة لبث مصدر واحد إلى عدة RTMP/RTMPS باستخدام **FFmpeg encode واحد فقط** لكل Stream.

## الفكرة

```text
M3U8 / HTTP / RTMP source
        |
        v
  FFmpeg filter/encode (ONCE)
  quality + bitrate + FPS
  logo + optional text
        |
        v
     tee muxer
   /     |      \
RTMPS  RTMPS   RTMP ...
```

كل مخارج الـStream تستخدم نفس الـencoded packets عبر FFmpeg `tee` muxer. لا يتم تشغيل encode منفصل لكل وجهة.

## الميزات

- مصدر URL واحد لكل Stream (`m3u8`, HTTP/HLS, RTMP/RTMPS وغير ذلك مما يدعمه FFmpeg).
- أكثر من Stream مستقل في نفس اللوحة.
- عدة RTMP/RTMPS destinations لكل Stream.
- `onfail=ignore` لكل destination: فشل مخرج لا يوقف البقية.
- `tee + fifo recovery`: يحاول إعادة ربط المخرج الفاشل بدون إعادة تشغيل encode.
- جودة: Original / 1080p / 720p / 480p.
- Bitrate وFPS وx264 preset.
- Logo من URL أو رفع PNG/JPG/WEBP.
- Text overlay اختياري.
- Start / Stop / Logs.
- Stream keys يتم حجبها من اللوج الذي تعرضه اللوحة.
- الإعدادات محفوظة في `./data/streams.json`.

## التثبيت على VPS Ubuntu

### 1) فك الضغط

```bash
unzip stream247-panel.zip
cd stream247-panel
```

### 2) التشغيل

المنفذ الافتراضي للوحة هو `28081`:

```bash
chmod +x install.sh
sudo ./install.sh
```

ثم افتح:

```text
http://VPS-IP:28081
```

لو `28081` مستخدم:

```bash
sudo PORT=38081 ./install.sh
```

### تشغيل يدوي

```bash
sudo docker compose up -d --build
```

### اللوج

```bash
sudo docker compose logs -f
```

### إيقاف اللوحة

```bash
sudo docker compose down
```

## حماية اللوحة بكلمة مرور

في `docker-compose.yml` فك التعليق عن:

```yaml
- ADMIN_USER=admin
- ADMIN_PASSWORD=change-me-now
```

ثم:

```bash
sudo docker compose up -d --build
```

يفضل وضع اللوحة خلف HTTPS إذا كانت متاحة من الإنترنت.

## Telegram

أضف Destination هكذا:

- RTMP/RTMPS Server: مثل القيمة التي يعطيك Telegram وتنتهي عادة بـ `/s/`
- Stream Key: المفتاح وحده

اللوحة تجمعهما داخليًا وتبني مخرج tee من الشكل:

```text
[f=flv:onfail=ignore]rtmps://SERVER/s/STREAM_KEY
```

## إعداد شبيه بالأمر الذي طلبته

- Quality: `1080p`
- Bitrate: `5000`
- FPS: `50`
- Preset: `superfast`
- Logo width: `335`
- Logo position: `top-right`
- Audio: AAC 128k / 48kHz / stereo
- GOP: FPS × 2 (`100` عند 50fps)
- Maxrate: bitrate × 1.10
- Bufsize: bitrate × 2

## ملاحظة حول الموارد

Encode واحد يعني أن إضافة 4 مخارج تلي لن تعمل 4 عمليات x264. لكن كل مخرج سيستهلك bandwidth مستقل. مثال: 5 Mbps إلى 4 مخارج ≈ 20 Mbps upload، إضافة للصوت والـoverhead.

## ملاحظة تشغيلية

اللوحة تستخدم `tee` مع `use_fifo=1` و`attempt_recovery=1`، لذلك المخرجات مفصولة عن encoder، وفشل أو بطء وجهة لا يفترض أن يوقف البقية، مع محاولات recovery تلقائية.
