# Stream247 — Single Encode / Tee + FIFO

هذه النسخة تعالج كل مصدر مرة واحدة فقط بواسطة عملية FFmpeg واحدة، ثم توزع نفس H.264/AAC إلى جميع مخارج RTMP/RTMPS بواسطة `tee` و`fifo`.

## ما تغير

- عملية FFmpeg واحدة فقط لكل Stream، مهما كان عدد المخارج.
- لا يوجد Python MPEG-TS fan-out ولا FFmpeg `-c copy` إضافي لكل مخرج.
- كل مخرج داخل FIFO مستقل مع recovery و`onfail=ignore` حتى لا يوقف بقية المخارج.
- Supervisor يعيد تشغيل Encoder تلقائيًا إذا خرج بسبب خطأ مؤقت في المصدر.
- زر الإيقاف لا ينتظر المخارج واحدًا وراء الآخر؛ يرسل SIGTERM مباشرة، ثم SIGKILL احتياطيًا بعد مهلة قصيرة بالخلفية.
- مراقبة CPU / RAM / Upload / Download / Disk للسيرفر.
- مراقبة CPU / RAM / FPS / Speed / Dropped frames / Restarts لكل FFmpeg.
- Stream Keys مخفية من السجلات.
- `streams.json` القديم متوافق، بما فيه الصيغة القديمة ذات مخرج واحد `rtmp_base` + `stream_key`.

## تحديث نسخة موجودة

احتفظ بمجلد `data/` لأنه يحتوي الإعدادات والصور، ثم استبدل ملفات المشروع بهذه النسخة وشغّل:

```bash
sudo docker compose down
sudo docker compose up -d --build
```

اللوحة افتراضيًا على:

```text
http://YOUR_VPS_IP:28081
```

## ملاحظة عن تعديل المخارج أثناء البث

هذه النسخة تعطي الأولوية للاستقرار وأقل استهلاك. قائمة مخارج `tee` تُبنى عند تشغيل FFmpeg، لذلك أوقف البث قبل إضافة/حذف/تغيير مخرج. إعادة الاتصال بالمخرج المتعطل تتم تلقائيًا داخل FIFO ولا تحتاج زر Reconnect منفرد.
