# Stream247 — Hot Outputs Edition

لوحة بث FFmpeg بمعالجة واحدة لكل مصدر ومخارج RTMP/RTMPS مستقلة.

## أهم التعديلات

- Encoder واحد فقط لكل بث.
- المخرجات تعمل بعمليات FFmpeg خفيفة `-c copy` بدون إعادة ترميز.
- إضافة/حذف/تغيير Stream Key أثناء البث بدون إعادة تشغيل الـEncoder.
- كل Output يعيد الاتصال تلقائياً لوحده عند الانقطاع.
- Queue مستقلة لكل Output حتى المخرج البطيء لا يوقف البقية.
- فحص المصدر من اللوحة بواسطة `ffprobe`: الدقة، FPS، codec، bitrate والصوت.
- زر لتطبيق إعدادات المصدر المقترحة على المعالجة.
- معاينة للشعار/الصورة عند الرفع.
- حالة كل Output في اللوحة: LIVE / CONNECTING / RECONNECT.
- إخفاء Stream Keys من السجلات.

## التشغيل

```bash
chmod +x install.sh
./install.sh
```

ثم افتح:

```text
http://YOUR_VPS_IP:28081
```

لبورت مختلف:

```bash
PORT=38081 ./install.sh
```

## تحديث نسخة موجودة على VPS

احتفظ بمجلد `data/` لأنه يحتوي إعداداتك والصور، ثم استبدل ملفات المشروع وأعد البناء:

```bash
docker compose down
docker compose up -d --build
```

صيغة `streams.json` القديمة بقيت متوافقة؛ لا تحتاج إعادة إدخال البثوث.
