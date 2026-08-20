# Stream247 Hot Outputs v2

لوحة FFmpeg لمعالجة مصدر واحد مرة واحدة ثم توزيع نفس الـencoded stream إلى عدة RTMP/RTMPS.

## الجديد
- تعديل/إضافة/حذف Stream Keys والمخارج أثناء البث بدون Restart للـEncoder.
- Hot Apply تلقائي أثناء تحرير المخارج والبث شغال.
- كل Output عملية FFmpeg خفيفة `-c copy` مستقلة.
- Reconnect تلقائي لكل Output كل ثانيتين عند الفصل.
- زر ↻ لإعادة تشغيل Output واحد يدويًا بدون لمس البقية.
- فحص المصدر بـ `ffprobe`: Resolution / FPS / Codec / Bitrate / Audio.
- زر تطبيق إعدادات المصدر على Quality/FPS/Bitrate.
- Preview للصورة عند الرفع أو وضع رابط مباشر.
- مفاتيح البث محجوبة من الـLogs.
- متوافق مع `data/streams.json` القديم.

## تثبيت جديد
```bash
chmod +x install.sh
sudo ./install.sh
```
الافتراضي: `http://VPS-IP:28081`

لبورت مختلف:
```bash
sudo PORT=38081 ./install.sh
```

## تحديث نسخة موجودة بدون خسارة إعداداتك
أوقف القديمة، احتفظ بمجلد `data/`، استبدل ملفات المشروع بهذه النسخة ثم أعد البناء:
```bash
sudo docker compose down
sudo docker compose up -d --build
```

إذا نقلت المشروع إلى مجلد جديد، انسخ `data/` القديم إلى المجلد الجديد قبل التشغيل.
